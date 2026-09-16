"""AfterMerge command line."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from aftermerge.detector import service as detector_service
from aftermerge.detector import windows
from aftermerge.detector.rules import SLO
from aftermerge.investigator import service as investigator_service
from aftermerge.investigator.code_map import DEFAULT_SOURCE_PREFIX
from aftermerge.report import render as report_render
from aftermerge.reproducer import capture as capture_mod
from aftermerge.reproducer.differential import (
    DEFAULT_REPEAT,
    DEFAULT_THRESHOLD,
    run_differential,
)
from aftermerge.reproducer.envelope import RequestEnvelope
from aftermerge.reproducer.verify import verify_differential
from aftermerge.store import db as store_db
from aftermerge.store.repositories import (
    CapturedRequestRepository,
    DeploymentRepository,
    FactRepository,
    IncidentRepository,
    VerificationRepository,
)
from aftermerge.telemetry import catalog, client
from aftermerge.testgen import context as testgen_context
from aftermerge.testgen.certify import certify as certify_test
from aftermerge.testgen.gate import run_gate
from aftermerge.testgen.generator import TemplateGenerator
from aftermerge.testgen.writer import write as write_candidate

app = typer.Typer(
    help="AfterMerge: closed-loop production regression pipeline.",
    no_args_is_help=True,
)
console = Console()

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
    for column in ("method", "path", "query", "seen", "replayable"):
        table.add_column(column, no_wrap=True)
    for row in rows:
        table.add_row(*row[:5])
    console.print(table)

    for row in rows:
        if row[5]:
            console.print(f"\n[yellow]{row[0]} {row[1]} not replayable:[/yellow] {row[5]}")


@app.command()
def replay(
    good: str | None = typer.Option(None, help="Baseline ref. Defaults to the incident's."),
    bad: str | None = typer.Option(None, help="Candidate ref. Defaults to the incident's."),
    repeat: int = typer.Option(DEFAULT_REPEAT, help="Requests to send against each side."),
    threshold: float = typer.Option(
        DEFAULT_THRESHOLD, help="Amplification ratio to call it reproduced."
    ),
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
def testgen() -> None:
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

    candidate = TemplateGenerator().generate(ctx)
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

    result = run_gate(test, good_ref=good, bad_ref=bad, repo_root=repo_root)

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
    max_attempts: int = typer.Option(3, help="Generation attempts before giving up."),
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
    outcome = certify_test(ctx, TemplateGenerator(), repo_root=repo_root, max_attempts=max_attempts)

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


if __name__ == "__main__":
    app()
