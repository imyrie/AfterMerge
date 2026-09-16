"""AfterMerge command line."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from aftermerge.telemetry import catalog, client

app = typer.Typer(help="AfterMerge: closed-loop production regression pipeline.", no_args_is_help=True)
console = Console()


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
