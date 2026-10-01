"""AfterMerge command line."""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from aftermerge import llm
from aftermerge.dataquality.checks import ADVISORY
from aftermerge.dataquality.runner import (
    DEFAULT_LOOKBACK_MINUTES,
    DEFAULT_MAX_STALENESS_MINUTES,
    QUERIES_DIR,
    run_all,
)
from aftermerge.detector import service as detector_service
from aftermerge.detector import windows
from aftermerge.detector.rules import SLO
from aftermerge.evaluation.harness import TASKS, TESTGEN, EvalRun, run_matrix
from aftermerge.investigator import service as investigator_service
from aftermerge.investigator.code_map import (
    DEFAULT_SOURCE_PREFIX,
    changed_files,
    diff_for,
)
from aftermerge.llm import TokenUsage
from aftermerge.patcher.fix import CANDIDATE_DIR, CANDIDATE_NAME, propose_fix
from aftermerge.patcher.patch import Patch, PatchRejected
from aftermerge.patcher.proposer import AnthropicProposer, PatchContext, RevertProposer
from aftermerge.patcher.pullrequest import (
    PullRequestError,
    create_branch,
    gh_command,
    infer_base,
    push_branch,
)
from aftermerge.patcher.validate import validate as validate_patch
from aftermerge.report import render as report_render
from aftermerge.report.pull_request import render_pull_request
from aftermerge.reproducer import capture as capture_mod
from aftermerge.reproducer.differential import (
    DEFAULT_REPEAT,
    DEFAULT_THRESHOLD,
    run_differential,
)
from aftermerge.reproducer.envelope import RequestEnvelope
from aftermerge.reproducer.sandbox import SEEDS, resolve_seed
from aftermerge.reproducer.verify import verify_differential
from aftermerge.store import db as store_db
from aftermerge.store.repositories import (
    CapturedRequestRepository,
    DeploymentRepository,
    FactRepository,
    HypothesisRepository,
    IncidentRepository,
    VerificationRepository,
)
from aftermerge.streaming.consumer import (
    DEFAULT_GROUP,
    DEFAULT_TOPIC,
    build_consumer,
    consume,
)
from aftermerge.streaming.windows import StreamState
from aftermerge.telemetry import catalog, client
from aftermerge.testgen import context as testgen_context
from aftermerge.testgen.certify import certify as certify_test
from aftermerge.testgen.gate import run_gate
from aftermerge.testgen.generator import AnthropicGenerator, TemplateGenerator
from aftermerge.testgen.writer import write as write_candidate
from aftermerge.warehouse import rollup as rollup_mod

# Read .env before any command touches os.environ. `.env.example` has documented
# ANTHROPIC_API_KEY since the model-backed paths landed, but nothing loaded the
# file, so putting a key there did nothing and the error message said it was
# unset. Loaded here rather than in llm.py because .env also carries the
# ClickHouse and Postgres settings other modules read.
load_dotenv()

SEED_HELP = (
    f"Sandbox dataset: {'/'.join(SEEDS)} or a path. Volume-dependent faults need a larger one."
)

app = typer.Typer(
    help="AfterMerge: closed-loop production regression pipeline.",
    no_args_is_help=True,
)
console = Console()

GENERATOR_HELP = "template (deterministic, no credentials) or anthropic (model-backed)."
PROPOSER_HELP = "revert (deterministic, no credentials) or anthropic (model-backed)."

deployments_app = typer.Typer(help="Record and inspect observed deploys.", no_args_is_help=True)
app.add_typer(deployments_app, name="deployments")


def _prepared_engine() -> object:
    """Create the database and tables if absent, then hand back an engine.

    Idempotent and cheap, so `deploy.sh` can call the CLI without a separate
    bootstrap step. Alembic takes this over when slice 1 adds more tables.
    """
    store_db.ensure_database()
    engine = store_db.get_engine()
    store_db.init_schema(engine)
    return engine


@deployments_app.command("record")
def deployments_record(
    service: str = typer.Option(..., help="Service that was deployed."),
    sha: str = typer.Option(..., help="Commit SHA now serving."),
    prev_sha: str | None = typer.Option(None, help="Commit SHA it replaced."),
    repo: str | None = typer.Option(None, help="Repository URL."),
    pr: int | None = typer.Option(None, help="Pull request number."),
    actor: str | None = typer.Option(None, help="Who deployed it."),
) -> None:
    """Record one observed deploy."""
    engine = _prepared_engine()
    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        deployment = DeploymentRepository(session).record(
            service=service,
            commit_sha=sha,
            prev_commit_sha=prev_sha,
            repo=repo,
            pr_number=pr,
            actor=actor,
        )
        console.print(f"recorded {deployment}")


@deployments_app.command("list")
def deployments_list(
    service: str | None = typer.Option(None, help="Filter to one service."),
    limit: int = typer.Option(20, help="Maximum rows."),
) -> None:
    """Show recent deploys."""
    engine = _prepared_engine()
    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        rows = DeploymentRepository(session).list_recent(service=service, limit=limit)

    if not rows:
        console.print("[yellow]no deploys recorded[/yellow]")
        return

    table = Table(title="deployments", title_justify="left", header_style="bold")
    for column in ("deployed_at", "service", "commit", "replaced"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row.deployed_at.isoformat(timespec="seconds"),
            row.service,
            row.commit_sha,
            row.prev_commit_sha or "-",
        )
    console.print(table)


def _render(result: client.FactResult, title: str) -> None:
    table = Table(title=title, title_justify="left", header_style="bold")
    for column in result.columns:
        table.add_column(column, justify="right" if column != result.columns[0] else "left")
    for row in result.rows:
        table.add_row(*(str(cell) for cell in row))
    console.print(table)


@app.command()
def queries() -> None:
    """List the fact catalog."""
    table = Table(title="fact catalog", title_justify="left", header_style="bold")
    table.add_column("name")
    table.add_column("description")
    for name in catalog.names():
        table.add_row(name, catalog.load(name).description)
    console.print(table)


@app.command()
def facts(
    service: str = typer.Option("orders", help="Service whose database spans to count."),
    route_service: str = typer.Option("gateway", help="Service serving the user-facing route."),
    route: str = typer.Option("GET /orders", help="Server span name for the route."),
    lookback_minutes: int = typer.Option(60, help="How far back to look."),
) -> None:
    """Run the slice 0 fact queries and print the results."""
    ch = client.get_client()

    spans = client.run(
        "span_count_per_trace", client=ch, service=service, lookback_minutes=lookback_minutes
    )
    latency = client.run(
        "latency_quantiles",
        client=ch,
        service=route_service,
        route=route,
        lookback_minutes=lookback_minutes,
    )

    if not spans and not latency:
        console.print("[yellow]no telemetry in the lookback window[/yellow]")
        raise typer.Exit(code=1)

    _render(spans, f"db spans per request — {service}")
    console.print()
    _render(latency, f"latency — {route_service} {route}")


@app.command()
def detect(
    service: str = typer.Option("orders", help="Service whose deploy is under test."),
    route_service: str = typer.Option("gateway", help="Service serving the user-facing route."),
    route: str = typer.Option("GET /orders", help="Server span name for the route."),
    p95_slo_ms: float = typer.Option(500.0, help="Latency objective for the route."),
    lookback_minutes: int = typer.Option(120, help="How far back to read telemetry."),
    min_samples: int = typer.Option(100, help="Required samples per side."),
) -> None:
    """Compare the two most recently deployed versions and open an incident if warranted."""
    engine = _prepared_engine()
    slo = SLO(route=route, p95_ms=p95_slo_ms)

    try:
        with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
            outcome = detector_service.detect(
                session,
                service=service,
                route_service=route_service,
                slo=slo,
                lookback_minutes=lookback_minutes,
                min_samples=min_samples,
            )
            incident_id = outcome.incident.id if outcome.incident else None
    except windows.NoComparisonAvailable as exc:
        console.print(f"[yellow]cannot compare:[/yellow] {exc}")
        raise typer.Exit(code=2) from exc

    c = outcome.detection.comparison
    table = Table(title="comparison", title_justify="left", header_style="bold")
    for column in ("", "baseline", "candidate"):
        table.add_column(column)
    table.add_row("version", outcome.window.baseline_version, outcome.window.candidate_version)
    table.add_row("samples", str(c.baseline_n), str(c.candidate_n))
    table.add_row("median ms", f"{c.baseline_median_ms:.1f}", f"{c.candidate_median_ms:.1f}")
    table.add_row("p95 ms", f"{c.baseline_p95_ms:.1f}", f"{c.candidate_p95_ms:.1f}")
    console.print(table)

    amp = outcome.detection.amplification
    if amp is not None:
        console.print(
            f"\ndb spans/req   {amp.baseline_per_request:.1f} -> "
            f"{amp.candidate_per_request:.1f}  ({amp.ratio:.1f}x)"
        )
    console.print(f"\np95 ratio      {c.ratio:.2f}x")
    console.print(f"Mann-Whitney p {c.p_value:.3e}")
    console.print(f"effect size    {c.effect_size:.3f}")

    colour = "red" if outcome.detection.triggered else "green"
    if outcome.detection.insufficient_data:
        colour = "yellow"
    console.print(f"\n[{colour}]{outcome.detection.headline}[/{colour}]")
    for reason in outcome.detection.reasons:
        console.print(f"  - {reason}")

    if incident_id:
        console.print(f"\nincident {incident_id} ({outcome.fact_count} facts recorded)")


@app.command()
def incidents(limit: int = typer.Option(10, help="Maximum rows.")) -> None:
    """List detected incidents and their evidence counts."""
    engine = _prepared_engine()
    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        rows = IncidentRepository(session).list_recent(limit=limit)
        rendered = [
            (
                row.detected_at.strftime("%Y-%m-%d %H:%M"),
                f"{row.service} {row.route}",
                row.severity,
                f"{row.baseline_version} -> {row.candidate_version}",
                str(len(FactRepository(session).for_incident(row.id))),
                row.summary,
            )
            for row in rows
        ]

    if not rendered:
        console.print("[yellow]no incidents recorded[/yellow]")
        return

    table = Table(title="incidents", title_justify="left", header_style="bold")
    for column in ("detected", "target", "severity", "versions", "facts"):
        table.add_column(column, no_wrap=True)
    for row in rendered:
        table.add_row(*row[:5])
    console.print(table)

    # Summaries concatenate every triggering reason, so they are printed as prose
    # beneath the table rather than folded into a column too narrow to read.
    for row in rendered:
        console.print(f"\n[bold]{row[1]}[/bold] ({row[2]})")
        for reason in row[5].split("; "):
            console.print(f"  - {reason}")


@app.command()
def investigate(
    service: str = typer.Option("orders", help="Service whose deploy is under test."),
    route_service: str = typer.Option("gateway", help="Service serving the user-facing route."),
    route: str = typer.Option("GET /orders", help="Server span name for the route."),
    p95_slo_ms: float = typer.Option(500.0, help="Latency objective for the route."),
    lookback_minutes: int = typer.Option(120, help="How far back to read telemetry."),
    source_prefix: str = typer.Option(
        DEFAULT_SOURCE_PREFIX,
        help="Repo path prefix stripped to match span code.file.path values.",
    ),
    output: Path | None = typer.Option(None, help="Write the markdown report here."),
    detect_first: bool = typer.Option(True, help="Run detection when no incident exists yet."),
) -> None:
    """Detect, gather evidence, correlate with the deploy, and write a report."""
    engine = _prepared_engine()
    repo_root = Path.cwd()

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        existing = IncidentRepository(session).list_recent(limit=1)
        incident = existing[0] if existing else None

        if incident is None:
            if not detect_first:
                console.print(
                    "[yellow]no incidents recorded; run `aftermerge detect` first[/yellow]"
                )
                raise typer.Exit(code=2)
            try:
                outcome = detector_service.detect(
                    session,
                    service=service,
                    route_service=route_service,
                    slo=SLO(route=route, p95_ms=p95_slo_ms),
                    lookback_minutes=lookback_minutes,
                )
            except windows.NoComparisonAvailable as exc:
                console.print(f"[yellow]cannot compare:[/yellow] {exc}")
                raise typer.Exit(code=2) from exc
            if outcome.incident is None:
                console.print(f"[green]{outcome.detection.headline}[/green]")
                for reason in outcome.detection.reasons:
                    console.print(f"  - {reason}")
                raise typer.Exit(code=0)
            incident = outcome.incident

        investigation = investigator_service.investigate(
            session, incident, repo_root=repo_root, source_prefix=source_prefix
        )
        markdown = report_render.render(investigation)

    if output is not None:
        output.write_text(markdown)
        console.print(f"report written to {output}")
    else:
        console.print(markdown)


@app.command()
def capture(
    route_service: str = typer.Option("gateway", help="Service whose inbound requests to capture."),
    lookback_minutes: int = typer.Option(120, help="How far back to read telemetry."),
    max_shapes: int = typer.Option(20, help="Maximum distinct request shapes to keep."),
) -> None:
    """Reconstruct replayable requests from the regressed version's telemetry."""
    engine = _prepared_engine()
    ch = client.get_client()

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        incidents_found = IncidentRepository(session).list_recent(limit=1)
        if not incidents_found:
            console.print("[yellow]no incidents recorded; run `aftermerge detect` first[/yellow]")
            raise typer.Exit(code=2)

        incident = incidents_found[0]
        capture_mod.capture_for_incident(
            session,
            incident,
            ch=ch,
            route_service=route_service,
            lookback_minutes=lookback_minutes,
            max_shapes=max_shapes,
        )
        rows = [
            (
                r.method,
                r.path,
                "&".join(f"{k}={v}" for k, v in r.query.items()) or "-",
                str(r.status_code or "-"),
                str(r.observations),
                "yes" if r.replay_safe else "no",
                r.unreplayable_reason or "",
            )
            for r in CapturedRequestRepository(session).for_incident(incident.id)
        ]

    if not rows:
        console.print("[yellow]no request shapes found in the lookback window[/yellow]")
        return

    table = Table(title="captured requests", title_justify="left", header_style="bold")
    for column in ("method", "path", "query", "status", "seen", "replayable"):
        table.add_column(column, no_wrap=True)
    for row in rows:
        table.add_row(*row[:6])
    console.print(table)

    for row in rows:
        if row[6]:
            console.print(f"\n[yellow]{row[0]} {row[1]} not replayable:[/yellow] {row[5]}")


@app.command()
def replay(
    good: str | None = typer.Option(None, help="Baseline ref. Defaults to the incident's."),
    bad: str | None = typer.Option(None, help="Candidate ref. Defaults to the incident's."),
    repeat: int = typer.Option(DEFAULT_REPEAT, help="Requests to send against each side."),
    threshold: float = typer.Option(
        DEFAULT_THRESHOLD, help="Amplification ratio to call it reproduced."
    ),
    seed: str | None = typer.Option(None, help=SEED_HELP),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Replay a captured request against two commits in isolation.

    Exits 0 when the regression reproduces and 1 when it does not. That exit
    code is the evidence: `aftermerge verify` records it rather than deciding
    for itself whether the replay succeeded.
    """
    engine = _prepared_engine()
    repo_root = Path.cwd()

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        found = IncidentRepository(session).list_recent(limit=1)
        if not found:
            console.print("[yellow]no incidents; run `aftermerge detect` first[/yellow]")
            raise typer.Exit(code=2)
        incident = found[0]
        good_ref = good or incident.baseline_version
        bad_ref = bad or incident.candidate_version

        replayable = CapturedRequestRepository(session).replayable_for_incident(incident.id)
        if not replayable:
            console.print(
                "[yellow]no replayable captured requests; run `aftermerge capture`[/yellow]"
            )
            raise typer.Exit(code=2)
        captured = replayable[0]
        envelope = RequestEnvelope(
            method=captured.method,
            path=captured.path,
            query=dict(captured.query),
            headers=dict(captured.headers),
            replay_safe=True,
            source_trace_id=captured.source_trace_id,
        )

    result = run_differential(
        envelope,
        good_ref=good_ref,
        bad_ref=bad_ref,
        repo_root=repo_root,
        repeat=repeat,
        threshold=threshold,
        seed=resolve_seed(seed),
    )

    if as_json:
        console.print_json(json.dumps(result.as_dict()))
    else:
        table = Table(
            title=f"differential replay \u2014 {result.target}",
            title_justify="left",
            header_style="bold",
        )
        for column in ("", good_ref, bad_ref):
            table.add_column(column)
        table.add_row(
            "db spans/request",
            f"{result.good.db_spans_per_request:.1f}",
            f"{result.bad.db_spans_per_request:.1f}",
        )
        table.add_row(
            "requests sent", str(result.good.requests_sent), str(result.bad.requests_sent)
        )
        table.add_row("failures", str(result.good.failures), str(result.bad.failures))
        table.add_row("code site", result.good.code_site or "-", result.bad.code_site or "-")
        console.print(table)
        colour = "red" if result.reproduced else "green"
        console.print(f"\n[{colour}]{result.summary}[/{colour}]")

    raise typer.Exit(code=0 if result.reproduced else 1)


@app.command()
def verify(
    repeat: int = typer.Option(DEFAULT_REPEAT, help="Requests to send against each side."),
) -> None:
    """Reproduce the incident in isolation and record the outcome as evidence.

    Runs the replay as a subprocess and stores its exit code. The verdict comes
    from what the process did, not from what this command believes.
    """
    engine = _prepared_engine()
    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        found = IncidentRepository(session).list_recent(limit=1)
        if not found:
            console.print("[yellow]no incidents; run `aftermerge detect` first[/yellow]")
            raise typer.Exit(code=2)

        console.print("running differential replay (two sandboxes, this takes a minute)...")
        try:
            verification = verify_differential(
                session, found[0], repo_root=Path.cwd(), repeat=repeat
            )
        except ValueError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(code=2) from exc

        verdict = verification.verdict
        exit_code = verification.exit_code
        summary = str(verification.metrics.get("summary", ""))

    colour = "red" if verdict == "confirmed" else "yellow"
    console.print(
        f"\n[{colour}]{verification.method}: {verdict}[/{colour}] (exit code {exit_code})"
    )
    if summary:
        console.print(summary)


@app.command()
def testgen(
    generator: str = typer.Option("template", help=GENERATOR_HELP),
) -> None:
    """Write a regression test encoding the incident's measured behaviour.

    Deterministic by default: the template generator needs no credentials, and
    a generated test is trustworthy because it passes the fail@bad / pass@good
    gate, not because of what wrote it.
    """
    engine = _prepared_engine()
    repo_root = Path.cwd()

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        found = IncidentRepository(session).list_recent(limit=1)
        if not found:
            console.print("[yellow]no incidents; run `aftermerge detect` first[/yellow]")
            raise typer.Exit(code=2)
        incident = found[0]

        replayable = CapturedRequestRepository(session).replayable_for_incident(incident.id)
        if not replayable:
            console.print(
                "[yellow]no replayable captured requests; run `aftermerge capture`[/yellow]"
            )
            raise typer.Exit(code=2)

        facts = FactRepository(session).for_incident(incident.id)
        investigation = investigator_service.investigate(
            session, incident, repo_root=repo_root, source_prefix=DEFAULT_SOURCE_PREFIX
        )
        ctx = testgen_context.build(incident, facts, replayable[0], investigation.correlation)

    if not ctx.discriminates:
        console.print(
            "[yellow]the evidence cannot support a discriminating test:[/yellow] "
            f"baseline {ctx.baseline_spans_per_request:.1f} vs candidate "
            f"{ctx.candidate_spans_per_request:.1f} operations per request"
        )
        raise typer.Exit(code=2)

    candidate = _build_generator(generator).generate(ctx)  # type: ignore[attr-defined]
    path = write_candidate(candidate, repo_root=repo_root)

    rel = path.relative_to(repo_root)
    console.print(f"wrote {rel}  [dim](by {candidate.generated_by})[/dim]")
    console.print(f"rationale: {candidate.rationale}\n")
    console.print("[dim]Not yet validated. Gate it with:[/dim]")
    run = f"uv run pytest -m slow {rel}"
    console.print(f"  AFTERMERGE_TEST_REF={ctx.candidate_version} {run}   # must FAIL")
    console.print(f"  AFTERMERGE_TEST_REF={ctx.baseline_version} {run}   # must PASS")


@app.command()
def gate(
    test: Path = typer.Option(..., help="Path to the generated test file."),
    good: str = typer.Option(..., help="Commit the test must PASS on."),
    bad: str = typer.Option(..., help="Commit the test must FAIL on."),
    seed: str | None = typer.Option(None, help=SEED_HELP),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Check that a test fails on the bad commit and passes on the good one.

    Exits 0 when it discriminates, 1 when it does not, 2 when the gate could not
    run at all. That exit code is the evidence.
    """
    repo_root = Path.cwd()
    if not (repo_root / test).exists() and not test.exists():
        console.print(f"[yellow]no such test file: {test}[/yellow]")
        raise typer.Exit(code=2)

    result = run_gate(
        test, good_ref=good, bad_ref=bad, repo_root=repo_root, seed=resolve_seed(seed)
    )

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "test_path": str(result.test_path),
                    "good_ref": good,
                    "bad_ref": bad,
                    "at_bad": {
                        "exit_code": result.at_bad.exit_code,
                        "satisfied": result.at_bad.satisfied,
                    },
                    "at_good": {
                        "exit_code": result.at_good.exit_code,
                        "satisfied": result.at_good.satisfied,
                    },
                    "passed": result.passed,
                    "reasons": list(result.reasons),
                    "summary": result.summary,
                }
            )
        )
    else:
        for reason in result.reasons:
            console.print(f"  - {reason}")
        colour = "green" if result.passed else "red"
        console.print(f"\n[{colour}]{result.summary}[/{colour}]")

    raise typer.Exit(code=0 if result.passed else 1)


@app.command()
def certify(
    generator: str = typer.Option("template", help=GENERATOR_HELP),
    max_attempts: int = typer.Option(3, help="Generation attempts before giving up."),
    seed: str | None = typer.Option(None, help=SEED_HELP),
) -> None:
    """Generate a regression test and keep it only if it passes the gate.

    A rejected candidate is deleted rather than left on disk: an ungated test in
    tests/regression/ is precisely the false assurance this is meant to prevent.
    """
    engine = _prepared_engine()
    repo_root = Path.cwd()

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        found = IncidentRepository(session).list_recent(limit=1)
        if not found:
            console.print("[yellow]no incidents; run `aftermerge detect` first[/yellow]")
            raise typer.Exit(code=2)
        incident = found[0]

        replayable = CapturedRequestRepository(session).replayable_for_incident(incident.id)
        if not replayable:
            console.print(
                "[yellow]no replayable captured requests; run `aftermerge capture`[/yellow]"
            )
            raise typer.Exit(code=2)

        facts = FactRepository(session).for_incident(incident.id)
        investigation = investigator_service.investigate(
            session, incident, repo_root=repo_root, source_prefix=DEFAULT_SOURCE_PREFIX
        )
        ctx = testgen_context.build(incident, facts, replayable[0], investigation.correlation)
        hypotheses = investigation.hypotheses

    if not ctx.discriminates:
        console.print(
            "[yellow]the evidence cannot support a discriminating test:[/yellow] "
            f"baseline {ctx.baseline_spans_per_request:.1f} vs candidate "
            f"{ctx.candidate_spans_per_request:.1f} operations per request"
        )
        raise typer.Exit(code=2)

    console.print("generating and gating (each attempt builds two sandboxes)...\n")
    outcome = certify_test(
        ctx,
        _build_generator(generator),  # type: ignore[arg-type]
        repo_root=repo_root,
        max_attempts=max_attempts,
    )

    for index, attempt in enumerate(outcome.attempts, start=1):
        mark = "accepted" if attempt.passed else "rejected"
        console.print(f"attempt {index} ({attempt.candidate.generated_by}): [bold]{mark}[/bold]")
        for reason in attempt.payload.get("reasons", []):
            console.print(f"  - {reason}")

    if not outcome.succeeded:
        console.print(f"\n[yellow]no test certified.[/yellow] {outcome.summary}")
        raise typer.Exit(code=1)

    # Record the gate as level-3 evidence. The verdict comes from the gate
    # process's exit code, not from this command's opinion of it.
    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        if hypotheses:
            VerificationRepository(session).record(
                hypothesis_id=hypotheses[0].id,
                method="regression_test_gate",
                process=outcome.accepted.process,  # type: ignore[union-attr]
                metrics=outcome.accepted.payload,  # type: ignore[union-attr]
                inconclusive_codes=frozenset({2}),
            )

    rel = outcome.test_path.relative_to(repo_root)  # type: ignore[union-attr]
    console.print(f"\n[green]certified[/green] {rel}")
    console.print(outcome.summary)


def _build_generator(name: str) -> object:
    """Pick a test generator, failing clearly when a model was asked for."""
    if name == "template":
        return TemplateGenerator()
    if name == "anthropic":
        try:
            return AnthropicGenerator(llm.get_client(), model=llm.model_name())
        except llm.LLMUnavailable as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(code=2) from exc
    raise typer.BadParameter(f"unknown generator {name!r}; use template or anthropic")


def _build_proposer(name: str, repo_root: Path) -> object:
    if name == "revert":
        return RevertProposer(repo_root)
    if name == "anthropic":
        try:
            return AnthropicProposer(llm.get_client(), repo_root=repo_root, model=llm.model_name())
        except llm.LLMUnavailable as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(code=2) from exc
    raise typer.BadParameter(f"unknown proposer {name!r}; use revert or anthropic")


def _scenario_verify_options(repo_root: Path) -> tuple[tuple[str, ...], list[str] | None]:
    """Normalisations and suite command, as declared in the scenario file."""
    import yaml

    path = repo_root / "scenarios" / "n_plus_one.yaml"
    if not path.is_file():
        return (), None
    data = yaml.safe_load(path.read_text()) or {}
    verify_block = data.get("verify") or {}
    return tuple(verify_block.get("normalisations") or ()), verify_block.get("suite_command")


@app.command()
def validate(
    patch: Path = typer.Option(..., help="Unified diff to validate."),
    strategy: str = typer.Option("repair", help="repair or revert -- stated in the PR."),
    origin: str = typer.Option("hand-written", help="What produced this patch."),
    repeat: int = typer.Option(10, help="Requests per side when measuring work."),
    seed: str | None = typer.Option(None, help=SEED_HELP),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Establish that a candidate fix removes the fault and changes nothing else.

    Exits 0 when the fix is validated, 1 when it is rejected, 2 when validation
    could not run. The regression test alone is not enough: a patch that returns
    fewer rows satisfies it and is broken, so responses are compared too.
    """
    engine = _prepared_engine()
    repo_root = Path.cwd()

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        found = IncidentRepository(session).list_recent(limit=1)
        if not found:
            console.print("[yellow]no incidents; run `aftermerge detect` first[/yellow]")
            raise typer.Exit(code=2)
        incident = found[0]
        replayable = CapturedRequestRepository(session).replayable_for_incident(incident.id)
        if not replayable:
            console.print(
                "[yellow]no replayable captured requests; run `aftermerge capture`[/yellow]"
            )
            raise typer.Exit(code=2)
        captured = replayable[0]
        facts = FactRepository(session).for_incident(incident.id)
        ctx = testgen_context.build(incident, facts, captured, None)

    test_path = Path("tests") / "regression" / f"{ctx.module_name}.py"
    if not (repo_root / test_path).is_file():
        console.print(
            f"[yellow]no certified test at {test_path}; run `aftermerge certify`[/yellow]"
        )
        raise typer.Exit(code=2)

    allowed = frozenset(
        c.repo_path
        for c in changed_files(
            incident.baseline_version, incident.candidate_version, repo_root=repo_root
        )
    )
    normalisations, suite_command = _scenario_verify_options(repo_root)
    envelope = RequestEnvelope(
        method=captured.method, path=captured.path, query=dict(captured.query), replay_safe=True
    )

    try:
        candidate = Patch.from_file(patch, strategy=strategy, origin=origin)
        result = validate_patch(
            candidate,
            envelope=envelope,
            good_ref=incident.baseline_version,
            bad_ref=incident.candidate_version,
            test_path=test_path,
            repo_root=repo_root,
            allowed_files=allowed,
            normalisations=normalisations,
            suite_command=suite_command,
            repeat=repeat,
            seed=resolve_seed(seed),
        )
    except PatchRejected as exc:
        if as_json:
            console.print_json(json.dumps({"passed": False, "rejected": str(exc)}))
        else:
            console.print(f"[red]patch rejected before running:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if as_json:
        console.print_json(json.dumps(result.as_dict()))
    else:
        for check in result.checks:
            colour = {"passed": "green", "failed": "red", "skipped": "yellow"}[check.status]
            console.print(f"  [{colour}]{check.status:8}[/{colour}] {check.name}: {check.detail}")
        colour = "green" if result.passed else "red"
        console.print(f"\n[{colour}]{result.summary}[/{colour}]")

    raise typer.Exit(code=0 if result.passed else 1)


@app.command()
def fix(
    proposer: str = typer.Option("revert", help=PROPOSER_HELP),
    max_attempts: int = typer.Option(3, help="Proposals before giving up."),
    seed: str | None = typer.Option(None, help=SEED_HELP),
) -> None:
    """Propose a fix and keep it only if validation accepts it.

    Defaults to a revert, which is a legitimate answer rather than a fallback:
    restoring the previous implementation always removes the regression, and
    costs only whatever else the commit was trying to do.
    """
    engine = _prepared_engine()
    repo_root = Path.cwd()

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        found = IncidentRepository(session).list_recent(limit=1)
        if not found:
            console.print("[yellow]no incidents; run `aftermerge detect` first[/yellow]")
            raise typer.Exit(code=2)
        incident = found[0]
        replayable = CapturedRequestRepository(session).replayable_for_incident(incident.id)
        if not replayable:
            console.print(
                "[yellow]no replayable captured requests; run `aftermerge capture`[/yellow]"
            )
            raise typer.Exit(code=2)
        facts = FactRepository(session).for_incident(incident.id)
        ctx = testgen_context.build(incident, facts, replayable[0], None)
        hypotheses = investigator_service.investigate(
            session, incident, repo_root=repo_root, source_prefix=DEFAULT_SOURCE_PREFIX
        ).hypotheses

    changed = changed_files(
        incident.baseline_version, incident.candidate_version, repo_root=repo_root
    )
    patch_context = PatchContext(
        good_ref=incident.baseline_version,
        bad_ref=incident.candidate_version,
        changed_files=tuple(c.repo_path for c in changed),
        code_site=ctx.code_site,
        baseline_spans_per_request=ctx.baseline_spans_per_request,
        candidate_spans_per_request=ctx.candidate_spans_per_request,
        causing_diff=diff_for(
            incident.baseline_version, incident.candidate_version, repo_root=repo_root
        ),
    )

    console.print("proposing and validating (each attempt builds three sandboxes)...\n")
    outcome = propose_fix(
        patch_context,
        _build_proposer(proposer, repo_root),  # type: ignore[arg-type]
        repo_root=repo_root,
        max_attempts=max_attempts,
        seed=seed,
    )

    for index, attempt in enumerate(outcome.attempts, start=1):
        mark = "accepted" if attempt.accepted else "rejected"
        label = f"{attempt.patch.origin}, {attempt.patch.strategy}"
        console.print(f"attempt {index} ({label}): [bold]{mark}[/bold]")
        for check in attempt.payload.get("checks", []):
            colour = {"passed": "green", "failed": "red", "skipped": "yellow"}[check["status"]]
            console.print(
                f"  [{colour}]{check['status']:8}[/{colour}] {check['name']}: {check['detail']}"
            )

    if not outcome.succeeded:
        console.print(f"\n[yellow]no fix validated.[/yellow] {outcome.summary}")
        raise typer.Exit(code=1)

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        if hypotheses:
            VerificationRepository(session).record(
                hypothesis_id=hypotheses[0].id,
                method="patch_validation",
                process=outcome.accepted.process,  # type: ignore[union-attr]
                metrics=outcome.accepted.payload,  # type: ignore[union-attr]
                inconclusive_codes=frozenset({2}),
            )

    rel = outcome.patch_path.relative_to(repo_root)  # type: ignore[union-attr]
    console.print(f"\n[green]validated[/green] {rel} ({outcome.accepted.patch.strategy})")  # type: ignore[union-attr]
    console.print(outcome.summary)


@app.command()
def pr(
    branch: str | None = typer.Option(None, help="Branch name. Defaults to aftermerge/fix-<sha>."),
    base: str | None = typer.Option(None, help="PR target branch. Inferred from the bad commit."),
    push: bool = typer.Option(False, "--push", help="Push the branch to the remote."),
    open_pr: bool = typer.Option(False, "--open", help="Also run `gh pr create --draft`."),
) -> None:
    """Build a branch and a pull request body from a validated fix.

    Local by default. A pull request notifies people and is awkward to retract,
    so going outward takes an explicit flag and opening one is never a side
    effect of an investigation. Nothing here merges anything, ever.
    """
    engine = _prepared_engine()
    repo_root = Path.cwd()
    patch_path = repo_root / CANDIDATE_DIR / CANDIDATE_NAME

    if not patch_path.is_file():
        console.print(
            "[yellow]no validated fix at .aftermerge/candidate.patch; run `aftermerge fix`[/yellow]"
        )
        raise typer.Exit(code=2)

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        found = IncidentRepository(session).list_recent(limit=1)
        if not found:
            console.print("[yellow]no incidents; run `aftermerge detect` first[/yellow]")
            raise typer.Exit(code=2)
        incident = found[0]

        validation: dict[str, object] = {}
        for hypothesis in HypothesisRepository(session).for_incident(incident.id):
            for verification in VerificationRepository(session).for_hypothesis(hypothesis.id):
                if verification.method == "patch_validation":
                    validation = dict(verification.metrics)
        if not validation:
            console.print(
                "[yellow]no recorded patch validation; run `aftermerge fix` first[/yellow]"
            )
            raise typer.Exit(code=2)

        captured = CapturedRequestRepository(session).for_incident(incident.id)
        replayable = [c for c in captured if c.replay_safe]
        investigation = investigator_service.investigate(
            session, incident, repo_root=repo_root, source_prefix=DEFAULT_SOURCE_PREFIX
        )
        strategy = str(validation.get("strategy") or "repair")
        candidate = Patch.from_file(patch_path, strategy=strategy, origin="aftermerge")

        message = (
            f"Fix {incident.severity} regression in {incident.service} {incident.route}\n\n"
            f"{strategy.capitalize()} of {incident.candidate_version}. Validated by replay:\n"
            f"{validation.get('summary', '')}\n"
        )
        mutating = bool(
            replayable and replayable[0].method.upper() not in {"GET", "HEAD", "OPTIONS"}
        )
        title, body = render_pull_request(
            investigation,
            strategy=strategy,
            validation=validation,
            diffstat="",
            request_shapes=len(captured),
            envelope_is_mutating=mutating,
        )

    target = base or infer_base(incident.candidate_version, repo_root=repo_root)
    name = branch or f"aftermerge/fix-{incident.candidate_version}"

    try:
        fix_branch = create_branch(
            candidate,
            bad_ref=incident.candidate_version,
            branch_name=name,
            repo_root=repo_root,
            message=message,
        )
    except PullRequestError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=2) from exc

    # Re-render now the diffstat exists, so the body describes the real branch.
    title, body = render_pull_request(
        investigation,
        strategy=strategy,
        validation=validation,
        diffstat=fix_branch.diffstat,
        request_shapes=len(captured),
        envelope_is_mutating=mutating,
    )
    body_path = repo_root / CANDIDATE_DIR / "pull_request.md"
    body_path.write_text(body)

    console.print(
        f"branch    [bold]{fix_branch.name}[/bold] at {fix_branch.sha} (off {fix_branch.base})"
    )
    console.print(f"base      {target}")
    console.print(f"title     {title}")
    console.print(f"body      {body_path.relative_to(repo_root)}  ({len(body.splitlines())} lines)")

    if push:
        push_branch(fix_branch, repo_root=repo_root)
        console.print(f"\n[green]pushed[/green] {fix_branch.name}")
    else:
        console.print(f"\n[dim]not pushed. To push:[/dim]  git push -u origin {fix_branch.name}")

    command = gh_command(fix_branch, target, title, body_path)
    if open_pr:
        if not push:
            console.print(
                "[yellow]--open requires --push; the branch is not on the remote[/yellow]"
            )
            raise typer.Exit(code=2)
        result = subprocess.run(command, cwd=repo_root, capture_output=True, text=True)
        if result.returncode != 0:
            console.print(f"[red]gh pr create failed:[/red] {result.stderr.strip()[:400]}")
            raise typer.Exit(code=1)
        console.print(f"[green]opened[/green] {result.stdout.strip()}")
    else:
        console.print("[dim]not opened. To open a draft PR:[/dim]")
        console.print("  " + " ".join(command))

    console.print("\n[dim]Nothing merges automatically. A person reviews and merges.[/dim]")


@app.command()
def evaluate(
    models: str = typer.Option(
        "claude-opus-5,claude-sonnet-5,claude-haiku-4-5-20251001",
        help="Comma-separated model ids to benchmark.",
    ),
    tasks: str = typer.Option("testgen,patch", help=f"Comma-separated: {', '.join(TASKS)}."),
    attempts: int = typer.Option(
        1, help="Attempts per run. 1 measures first-attempt acceptance; more measures retries."
    ),
    seed: str | None = typer.Option(None, help=SEED_HELP),
    out: Path | None = typer.Option(None, help="Write the full results as JSON here."),
) -> None:
    """Benchmark models on how often their output survives the gate.

    Every run goes through the same validation the pipeline uses, so "accepted"
    means what it means in production. Cost per accepted result is the headline:
    a cheap model that is never accepted costs more per useful result than an
    expensive one that is.
    """
    engine = _prepared_engine()
    repo_root = Path.cwd()
    model_list = [m.strip() for m in models.split(",") if m.strip()]
    task_list = [t.strip() for t in tasks.split(",") if t.strip()]
    for task in task_list:
        if task not in TASKS:
            raise typer.BadParameter(f"unknown task {task!r}; use {' or '.join(TASKS)}")

    with store_db.session_scope(engine) as session:  # type: ignore[arg-type]
        found = IncidentRepository(session).list_recent(limit=1)
        if not found:
            console.print("[yellow]no incidents; run `aftermerge detect` first[/yellow]")
            raise typer.Exit(code=2)
        incident = found[0]
        replayable = CapturedRequestRepository(session).replayable_for_incident(incident.id)
        if not replayable:
            console.print(
                "[yellow]no replayable captured requests; run `aftermerge capture`[/yellow]"
            )
            raise typer.Exit(code=2)
        facts = FactRepository(session).for_incident(incident.id)
        ctx = testgen_context.build(incident, facts, replayable[0], None)

    changed = changed_files(
        incident.baseline_version, incident.candidate_version, repo_root=repo_root
    )
    patch_context = PatchContext(
        good_ref=incident.baseline_version,
        bad_ref=incident.candidate_version,
        changed_files=tuple(c.repo_path for c in changed),
        code_site=ctx.code_site,
        baseline_spans_per_request=ctx.baseline_spans_per_request,
        candidate_spans_per_request=ctx.candidate_spans_per_request,
        causing_diff=diff_for(
            incident.baseline_version, incident.candidate_version, repo_root=repo_root
        ),
    )

    # Benchmarking overwrites the certified test and the candidate patch. Snapshot
    # both, restore the test before every patch run so each model starts from the
    # same gate, and put the originals back at the end -- a benchmark should not
    # leave the repository in a different state than it found it.
    test_path = repo_root / "tests" / "regression" / f"{ctx.module_name}.py"
    original_test = test_path.read_text() if test_path.is_file() else None
    patch_path = repo_root / CANDIDATE_DIR / CANDIDATE_NAME
    original_patch = patch_path.read_text() if patch_path.is_file() else None

    def run_one(model: str, task: str) -> EvalRun:
        started = time.monotonic()
        if task == TESTGEN:
            outcome = certify_test(
                ctx,
                AnthropicGenerator(llm.get_client(), model=model),
                repo_root=repo_root,
                max_attempts=attempts,
                seed=seed,
            )
            usage = TokenUsage()
            for attempt in outcome.attempts:
                usage = usage + attempt.candidate.usage
            return EvalRun(
                model=model,
                task=task,
                accepted=outcome.succeeded,
                attempts=len(outcome.attempts),
                usage=usage,
                seconds=time.monotonic() - started,
                detail=outcome.summary,
            )

        # The patch task validates against a certified test, so restore the
        # known-good one first; otherwise a model is judged on whichever test the
        # previous run happened to leave behind.
        if original_test is not None:
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(original_test)
        outcome_fix = propose_fix(
            patch_context,
            AnthropicProposer(llm.get_client(), repo_root=repo_root, model=model),
            repo_root=repo_root,
            max_attempts=attempts,
            seed=seed,
        )
        usage = TokenUsage()
        for attempt_fix in outcome_fix.attempts:
            usage = usage + attempt_fix.patch.usage
        return EvalRun(
            model=model,
            task=task,
            accepted=outcome_fix.succeeded,
            attempts=len(outcome_fix.attempts),
            usage=usage,
            seconds=time.monotonic() - started,
            detail=outcome_fix.summary,
        )

    def announce(model: str, task: str) -> None:
        console.print(f"[dim]running {model} / {task} ...[/dim]")

    try:
        report = run_matrix(model_list, task_list, run_one, on_start=announce)
    finally:
        if original_test is not None:
            test_path.write_text(original_test)
        if original_patch is not None:
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text(original_patch)
        elif patch_path.is_file():
            patch_path.unlink()

    table = Table(title="model evaluation", title_justify="left", header_style="bold")
    for column in ("model", "task", "accepted", "attempts", "tokens", "cost", "secs"):
        table.add_column(column, no_wrap=True)
    for run in report.runs:
        cost = f"${run.cost_usd:.4f}" if run.cost_usd is not None else "-"
        table.add_row(
            run.model,
            run.task,
            "yes" if run.accepted else "no",
            str(run.attempts),
            f"{run.usage.total:,}",
            cost,
            f"{run.seconds:.0f}",
        )
    console.print(table)

    summary = Table(title="per model", title_justify="left", header_style="bold")
    for column in ("model", "acceptance", "tokens", "cost", "cost / accepted"):
        summary.add_column(column, no_wrap=True)
    for model in report.models:
        total_cost = report.cost(model)
        per_accepted = report.cost_per_accepted(model)
        summary.add_row(
            model,
            f"{report.acceptance_rate(model):.0%}",
            f"{report.total_usage(model).total:,}",
            f"${total_cost:.4f}" if total_cost is not None else "-",
            f"${per_accepted:.4f}" if per_accepted is not None else "never accepted",
        )
    console.print(summary)

    if out is not None:
        out.write_text(json.dumps(report.as_dict(), indent=2))
        console.print(f"\nwrote {out}")


@app.command()
def dq(
    lookback_minutes: int = typer.Option(DEFAULT_LOOKBACK_MINUTES, help="Analysis window."),
    max_staleness_minutes: int = typer.Option(
        DEFAULT_MAX_STALENESS_MINUTES, help="How old the newest span may be."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Check the data before anything draws conclusions from it.

    Exits 0 when the data is trustworthy, 1 when a blocking check fails, 2 when
    no check could produce evidence. A blocking failure should stop downstream
    analysis: a wrong number that looks plausible is worse than a missing one.

    An advisory finding prints but does not change the exit code -- it means one
    metric needs a caveat, not that the data is unfit to analyse.
    """
    try:
        ch: object | None = client.get_client()
    except Exception:
        ch = None

    session_cm = None
    session: object | None = None
    try:
        engine = store_db.get_engine()
        session_cm = store_db.session_scope(engine)
        session = session_cm.__enter__()
    except Exception:
        session_cm, session = None, None

    try:
        report = run_all(
            ch=ch,
            session=session,
            queries_dir=Path(QUERIES_DIR),
            lookback_minutes=lookback_minutes,
            max_staleness_minutes=max_staleness_minutes,
        )
    finally:
        if session_cm is not None:
            with contextlib.suppress(Exception):
                session_cm.__exit__(None, None, None)

    if as_json:
        console.print_json(json.dumps(report.as_dict()))
    else:
        table = Table(title="data quality", title_justify="left", header_style="bold")
        for column in ("check", "status", "detail"):
            table.add_column(column, overflow="fold")
        for result in report.results:
            # An advisory failure gets its own label: rendering it as a red
            # "failed" next to an exit code of 0 reads like a bug in the tool.
            label = "advisory" if (result.failed and result.severity == ADVISORY) else result.status
            colour = {
                "passed": "green",
                "failed": "red",
                "skipped": "yellow",
                "advisory": "yellow",
            }[label]
            table.add_row(result.name, f"[{colour}]{label}[/{colour}]", result.detail)
        console.print(table)
        colour = "green" if report.passed else "red"
        console.print(f"\n[{colour}]{report.summary}[/{colour}]")

    if report.passed:
        raise typer.Exit(code=0)
    raise typer.Exit(code=1 if report.blocking_failures else 2)


warehouse_app = typer.Typer(help="Manage the telemetry warehouse.", no_args_is_help=True)
app.add_typer(warehouse_app, name="warehouse")


@warehouse_app.command("apply")
def warehouse_apply(
    database: str | None = typer.Option(
        None, help="Target database. Defaults to the configured one."
    ),
    recreate: bool = typer.Option(
        False, help="Drop and recreate the rollup tables. Needed after a schema change."
    ),
) -> None:
    """Create the rollup tables.

    Loading them is a separate step (`warehouse refresh`), because creating a
    table and filling it have different failure modes and a scheduler should be
    able to retry the second without re-running the first.
    """
    ch = client.get_client(database)
    if recreate:
        for table in rollup_mod.LOADS:
            ch.command(f"DROP TABLE IF EXISTS {table}")
        console.print(f"dropped {len(rollup_mod.LOADS)} rollup table(s)")
    applied = rollup_mod.apply(ch)
    console.print(f"applied {applied} statement(s)")


@warehouse_app.command("refresh")
def warehouse_refresh(
    database: str | None = typer.Option(None, help="Target database."),
    full: bool = typer.Option(False, help="Rebuild every day rather than only what changed."),
    lookback_days: int = typer.Option(
        rollup_mod.DEFAULT_LOOKBACK_DAYS,
        help="Days behind the watermark to reload anyway, for late-arriving spans.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Bring the rollups up to date, reprocessing only the days that changed.

    Idempotent: each day is dropped and rebuilt, so re-running produces the same
    result. That is what makes the step safe for a scheduler to retry.
    """
    ch = client.get_client(database)
    report = rollup_mod.refresh(ch, full=full, lookback_days=lookback_days)

    if as_json:
        console.print_json(json.dumps(report.as_dict()))
    else:
        table = Table(title="rollup refresh", title_justify="left", header_style="bold")
        for column in ("table", "days", "rows scanned", "rows written"):
            table.add_column(column, no_wrap=True)
        for load in report.loads:
            table.add_row(
                load.table,
                str(len(load.days)) if load.days else "-",
                f"{load.rows_scanned:,}",
                f"{load.rows_written:,}",
            )
        console.print(table)
        console.print(f"\n{report.summary}")


@warehouse_app.command("benchmark")
def warehouse_benchmark(
    service: str = typer.Option("orders", help="Service for the db-work question."),
    route_service: str = typer.Option("gateway", help="Service for the latency question."),
    route: str = typer.Option("GET /orders", help="Route for the latency question."),
    days: int = typer.Option(7, help="Window, in days."),
    database: str | None = typer.Option(
        None, help="Target database. Defaults to the configured one."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Compare rollup-backed queries against raw, on answer and on cost.

    Equivalence is checked first. A rollup that is fast and wrong is worse than
    no rollup, so a speedup is only reported for questions that agree.
    """
    ch = client.get_client(database)
    minutes = days * 24 * 60
    params = {
        "route latency": (
            {"service": route_service, "route": route, "lookback_minutes": minutes},
            {"service": route_service, "route": route, "lookback_days": days},
        ),
        "db work per request": (
            {"service": service, "lookback_minutes": minutes},
            {"service": service, "lookback_days": days},
        ),
    }

    comparisons = []
    for question, raw_name, rollup_name in rollup_mod.EQUIVALENTS:
        raw_params, rollup_params = params[question]
        comparisons.append(
            rollup_mod.compare(ch, question, raw_name, raw_params, rollup_name, rollup_params)
        )
    benchmark = rollup_mod.RollupBenchmark(comparisons=tuple(comparisons))

    if as_json:
        console.print_json(json.dumps(benchmark.as_dict()))
    else:
        table = Table(title="rollup vs raw", title_justify="left", header_style="bold")
        for column in ("question", "agrees", "rows read", "bytes read", "ms", "speedup"):
            table.add_column(column, no_wrap=True)
        for c in comparisons:
            table.add_row(
                c.question,
                "yes" if c.equivalent else "NO",
                f"{c.raw.rows_read:,} -> {c.rollup.rows_read:,}",
                f"{c.raw.bytes_read:,} -> {c.rollup.bytes_read:,}",
                f"{c.raw.elapsed_ms:.0f} -> {c.rollup.elapsed_ms:.0f}",
                f"{c.speedup:.1f}x",
            )
        console.print(table)
        for c in comparisons:
            if not c.equivalent:
                console.print(f"[red]{c.question} disagrees:[/red] {c.detail}")
        colour = "green" if benchmark.all_equivalent else "red"
        state = "all questions agree" if benchmark.all_equivalent else "ROLLUP DISAGREES WITH RAW"
        console.print(f"\n[{colour}]{state}[/{colour}]")

    raise typer.Exit(code=0 if benchmark.all_equivalent else 1)


@app.command()
def stream(
    service: str = typer.Option("orders", help="Service whose database work to watch."),
    route_service: str = typer.Option("gateway", help="Service serving the route."),
    route: str = typer.Option("GET /orders", help="Server span name for the route."),
    p95_slo_ms: float = typer.Option(500.0, help="Latency objective for the route."),
    topic: str = typer.Option(DEFAULT_TOPIC, help="Kafka topic carrying spans."),
    group: str = typer.Option(DEFAULT_GROUP, help="Consumer group id."),
    from_beginning: bool = typer.Option(False, help="Replay the topic from the start."),
    min_samples: int = typer.Option(30, help="Samples per side before comparing."),
    evaluate_every: int = typer.Option(200, help="Spans between evaluations."),
    max_spans: int | None = typer.Option(None, help="Stop after this many spans."),
    idle_timeout: float | None = typer.Option(
        None, help="Stop after this many seconds with no messages. Needed to replay a topic."
    ),
) -> None:
    """Watch spans arrive and reach a verdict during the rollout.

    The same rules the batch detector uses, applied to rolling windows instead of
    a completed deploy. It is an early signal on thin evidence, not a replacement
    for `aftermerge detect`, which remains what opens an incident.
    """
    state = StreamState(service=service, route_service=route_service, route=route)
    slo = SLO(route=route, p95_ms=p95_slo_ms)

    console.print(
        f"consuming [bold]{topic}[/bold] as {group}; "
        f"watching {route_service} {route} and {service} database work"
    )
    console.print("[dim]ctrl-c to stop[/dim]\n")

    try:
        consumer = build_consumer(group, from_beginning=from_beginning)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[yellow]cannot reach Kafka:[/yellow] {exc}")
        raise typer.Exit(code=2) from exc

    triggered = False
    try:
        for verdict in consume(
            state,
            slo,
            consumer=consumer,
            topic=topic,
            min_samples=min_samples,
            evaluate_every=evaluate_every,
            max_messages=max_spans,
            idle_timeout=idle_timeout,
        ):
            detection = verdict.detection
            colour = "red" if detection.triggered else "green"
            console.print(
                f"[{colour}]{detection.headline}[/{colour}] "
                f"({verdict.baseline} -> {verdict.candidate}, {verdict.spans_seen:,} spans)"
            )
            for reason in detection.reasons:
                console.print(f"  - {reason}")
            triggered = triggered or detection.triggered
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")

    raise typer.Exit(code=1 if triggered else 0)


if __name__ == "__main__":
    app()
