"""AfterMerge command line."""

from __future__ import annotations

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
from aftermerge.store import db as store_db
from aftermerge.store.repositories import DeploymentRepository, FactRepository, IncidentRepository
from aftermerge.telemetry import catalog, client

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


if __name__ == "__main__":
    app()


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
