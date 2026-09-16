"""AfterMerge command line."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from aftermerge.store import db as store_db
from aftermerge.store.repositories import DeploymentRepository
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
