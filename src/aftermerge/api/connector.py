"""Async ClickHouse access for the API.

Serves the *catalog* -- the same named, parameterised .sql files the pipeline
runs -- rather than SQL written inline here. That is the point of the catalog:
if the API computed p95 its own way, an answer from the API and an answer from
`aftermerge facts` could differ, and neither would be wrong enough to notice.

`clickhouse_connect.get_async_client()` is a real async client, so a slow scan
suspends the request rather than blocking the event loop and stalling every
other caller.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import clickhouse_connect

from aftermerge.telemetry import catalog


@dataclass(frozen=True)
class MetricResult:
    """A metric value and the statement that produced it.

    Provenance travels with the payload. Every number this API returns names the
    query and parameters it came from, so a caller can reproduce it without
    asking anyone -- the same contract the audit trail enforces internally.
    """

    query_name: str
    params: dict[str, Any]
    columns: list[str]
    rows: list[list[Any]]
    read_rows: int | None = None
    cached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "query_name": self.query_name,
            "params": self.params,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "read_rows": self.read_rows,
            "cached": self.cached,
        }


class Warehouse(Protocol):
    """What the API needs from ClickHouse, so tests can supply it directly."""

    async def run(self, name: str, **params: Any) -> MetricResult: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


@dataclass
class ClickHouseWarehouse:
    """The real thing: one async client, reused across requests."""

    client: Any = None
    settings: dict[str, Any] = field(
        # The API is a read path. These are the same caps the generated-SQL
        # executor applies, for the same reason: a reporting call should not be
        # able to occupy the cluster.
        default_factory=lambda: {
            "readonly": 1,
            "max_execution_time": 20,
            "max_result_rows": 5_000,
            "result_overflow_mode": "break",
        }
    )

    async def connect(self) -> None:
        self.client = await clickhouse_connect.get_async_client(
            host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            database=os.environ.get("CLICKHOUSE_DATABASE", "otel"),
        )

    async def run(self, name: str, **params: Any) -> MetricResult:
        query = catalog.load(name)
        result = await self.client.query(query.sql, parameters=params, settings=self.settings)
        summary = dict(result.summary or {})
        read_rows = int(summary["read_rows"]) if "read_rows" in summary else None
        return MetricResult(
            query_name=name,
            params=params,
            columns=list(result.column_names),
            rows=[list(row) for row in result.result_rows],
            read_rows=read_rows,
        )

    async def ping(self) -> bool:
        try:
            await self.client.query("SELECT 1", settings=self.settings)
        except Exception:  # noqa: BLE001 - health is a boolean, not an exception
            return False
        return True

    async def close(self) -> None:
        """Await the close, because the async client's is a coroutine.

        Calling it without awaiting returned an un-awaited coroutine and leaked
        the underlying aiohttp session and TCP connector on every shutdown --
        silent apart from a RuntimeWarning.
        """
        if self.client is None:
            return
        closing = self.client.close()
        if inspect.isawaitable(closing):
            await closing
        self.client = None
