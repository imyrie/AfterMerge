"""The read API: caching, bounds, provenance, and what it reports as healthy."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aftermerge.api.app import cache_key, create_app
from aftermerge.api.cache import TTLCache
from aftermerge.api.connector import ClickHouseWarehouse, MetricResult


class FakeWarehouse:
    """Counts calls, so a cache hit is provable rather than assumed."""

    def __init__(self, *, reachable: bool = True, fail: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reachable = reachable
        self.fail = fail

    async def run(self, name: str, **params: Any) -> MetricResult:
        self.calls.append((name, params))
        if self.fail:
            raise RuntimeError(self.fail)
        return MetricResult(
            query_name=name,
            params=params,
            columns=["version", "p95_ms"],
            rows=[["cbb4790", 515.0], ["8b4fd77", 2510.5]],
            read_rows=8192,
        )

    async def ping(self) -> bool:
        return self.reachable

    async def close(self) -> None:
        return None


def client_for(warehouse: FakeWarehouse, **kwargs: Any) -> TestClient:
    return TestClient(create_app(warehouse=warehouse, **kwargs))


# --- the cache ----------------------------------------------------------------


def test_a_repeated_question_is_served_from_cache() -> None:
    warehouse = FakeWarehouse()
    with client_for(warehouse) as client:
        first = client.get("/metrics/latency").json()
        second = client.get("/metrics/latency").json()

    assert first["cached"] is False
    assert second["cached"] is True
    assert len(warehouse.calls) == 1


def test_different_parameters_are_different_cache_entries() -> None:
    warehouse = FakeWarehouse()
    with client_for(warehouse) as client:
        client.get("/metrics/latency", params={"lookback_minutes": 60})
        client.get("/metrics/latency", params={"lookback_minutes": 120})

    assert len(warehouse.calls) == 2


def test_the_cache_key_ignores_parameter_order() -> None:
    assert cache_key("q", {"a": 1, "b": 2}) == cache_key("q", {"b": 2, "a": 1})


def test_an_expired_entry_is_reloaded() -> None:
    warehouse = FakeWarehouse()
    with client_for(warehouse, ttl_seconds=0) as client:
        client.get("/metrics/latency")
        second = client.get("/metrics/latency").json()

    assert second["cached"] is False
    assert len(warehouse.calls) == 2


def test_the_stored_entry_is_not_mutated_to_say_it_was_a_hit() -> None:
    """`cached` describes the response, not the stored copy."""
    warehouse = FakeWarehouse()
    with client_for(warehouse) as client:
        client.get("/metrics/latency")
        a = client.get("/metrics/latency").json()
        b = client.get("/metrics/latency").json()
    assert a["cached"] is True and b["cached"] is True


def test_concurrent_cold_requests_issue_one_load() -> None:
    """The moment a cache matters most is the moment it would otherwise do nothing.

    Without single-flight, N concurrent requests for one cold key run N
    identical warehouse scans.
    """
    loads = 0

    async def slow_load() -> str:
        nonlocal loads
        loads += 1
        await asyncio.sleep(0.05)
        return "value"

    async def exercise() -> list[tuple[str, bool]]:
        cache: TTLCache[str] = TTLCache(ttl_seconds=60)
        return list(await asyncio.gather(*(cache.get_or_load("k", slow_load) for _ in range(10))))

    results = asyncio.run(exercise())
    assert loads == 1
    assert all(value == "value" for value, _ in results)


def test_a_failed_load_does_not_poison_the_key() -> None:
    """A transient warehouse error must not make the key permanently unloadable."""
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("warehouse down")
        return "recovered"

    async def exercise() -> str:
        cache: TTLCache[str] = TTLCache(ttl_seconds=60)
        with pytest.raises(RuntimeError):
            await cache.get_or_load("k", flaky)
        value, _ = await cache.get_or_load("k", flaky)
        return value

    assert asyncio.run(exercise()) == "recovered"


def test_the_cache_is_bounded() -> None:
    async def exercise() -> int:
        cache: TTLCache[int] = TTLCache(ttl_seconds=60, max_entries=3)
        for i in range(10):
            await cache.get_or_load(f"k{i}", lambda i=i: _immediate(i))  # type: ignore[misc]
        return cache.size

    assert asyncio.run(exercise()) == 3


async def _immediate(value: int) -> int:
    return value


def test_cache_stats_are_reported() -> None:
    warehouse = FakeWarehouse()
    with client_for(warehouse) as client:
        client.get("/metrics/latency")
        client.get("/metrics/latency")
        stats = client.get("/cache").json()

    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["entries"] == 1


def test_closing_the_warehouse_awaits_the_async_close() -> None:
    """Measured: the async client's close() is a coroutine.

    Calling it without awaiting left an un-awaited coroutine and leaked the
    aiohttp session and TCP connector on every shutdown, announced only by a
    RuntimeWarning.
    """
    closed = False

    class AsyncCloseClient:
        async def close(self) -> None:
            nonlocal closed
            closed = True

    warehouse = ClickHouseWarehouse(client=AsyncCloseClient())
    asyncio.run(warehouse.close())
    assert closed
    assert warehouse.client is None


def test_closing_an_unconnected_warehouse_is_a_no_op() -> None:
    asyncio.run(ClickHouseWarehouse().close())


# --- bounds -------------------------------------------------------------------


def test_an_unbounded_lookback_is_rejected() -> None:
    """A full-retention scan must not be reachable from a URL."""
    warehouse = FakeWarehouse()
    with client_for(warehouse) as client:
        assert (
            client.get("/metrics/latency", params={"lookback_minutes": 99_999_999}).status_code
            == 422
        )
        assert client.get("/metrics/latency", params={"lookback_minutes": 0}).status_code == 422
        assert (
            client.get("/metrics/latency/trend", params={"lookback_days": 100_000}).status_code
            == 422
        )
    assert warehouse.calls == []


# --- provenance and health ----------------------------------------------------


def test_every_response_names_the_statement_that_produced_it() -> None:
    warehouse = FakeWarehouse()
    with client_for(warehouse) as client:
        body = client.get("/metrics/latency", params={"service": "gateway"}).json()

    assert body["query_name"] == "latency_quantiles"
    assert body["params"]["service"] == "gateway"
    assert body["read_rows"] == 8192
    assert body["row_count"] == 2


def test_the_trend_endpoint_runs_the_window_function_query() -> None:
    warehouse = FakeWarehouse()
    with client_for(warehouse) as client:
        body = client.get("/metrics/latency/trend").json()

    assert body["query_name"] == "route_latency_trend"
    assert warehouse.calls[0][0] == "route_latency_trend"


def test_the_catalog_documents_each_metric_and_its_parameters() -> None:
    """Served from the .sql files, so the documentation cannot drift."""
    warehouse = FakeWarehouse()
    with client_for(warehouse) as client:
        metrics = client.get("/catalog").json()["metrics"]

    by_name = {m["name"]: m for m in metrics}
    assert "route_latency_trend" in by_name
    assert by_name["latency_quantiles"]["parameters"]["lookback_minutes"] == "UInt32"
    assert by_name["latency_quantiles"]["description"]


def test_health_reports_the_warehouse_not_just_the_process() -> None:
    """A check that only proves the web server started stays green through an outage."""
    with client_for(FakeWarehouse(reachable=True)) as client:
        assert client.get("/health").json() == {"status": "ok", "warehouse": True}
    with client_for(FakeWarehouse(reachable=False)) as client:
        assert client.get("/health").json() == {"status": "degraded", "warehouse": False}


def test_a_warehouse_failure_is_a_502_not_a_traceback() -> None:
    warehouse = FakeWarehouse(fail="TOO_MANY_ROWS")
    with client_for(warehouse) as client:
        response = client.get("/metrics/latency")

    assert response.status_code == 502
    assert "TOO_MANY_ROWS" in response.json()["detail"]
