"""ClickHouse access for the fact catalog."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import clickhouse_connect

from aftermerge.telemetry import catalog


@dataclass(frozen=True)
class FactResult:
    """A query result plus everything needed to reproduce it."""

    query_name: str
    params: dict[str, Any]
    columns: list[str]
    rows: list[tuple[Any, ...]]

    def __bool__(self) -> bool:
        return bool(self.rows)


def get_client(database: str | None = None) -> Any:
    """A ClickHouse client, optionally pointed at a sandbox's trace database.

    The `database` argument is what lets replay run the *same* named SQL as
    production. If replay used different queries, a difference between the two
    would prove nothing about the code.
    """
    return clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        database=database or os.environ.get("CLICKHOUSE_DATABASE", "otel"),
    )


def run(name: str, client: Any | None = None, **params: Any) -> FactResult:
    query = catalog.load(name)
    client = client or get_client()
    result = client.query(query.sql, parameters=params)
    return FactResult(
        query_name=name,
        params=params,
        columns=list(result.column_names),
        rows=[tuple(r) for r in result.result_rows],
    )
