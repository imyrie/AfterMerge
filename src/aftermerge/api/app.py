"""Read API over the telemetry warehouse.

Every endpoint is a catalog query with validated parameters, a cache in front,
and provenance in the response. Nothing here writes, and nothing here invents
SQL: an answer from this API and an answer from `aftermerge facts` are the same
statement with the same parameters, which is the only way two surfaces reporting
the same metric can be trusted to agree.

Bounds on the query parameters are load-bearing rather than defensive polish.
`lookback_minutes` with no ceiling is a full-retention scan reachable from a URL,
and the caps in the connector would then be the only thing between a dashboard
refresh and the cluster.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from aftermerge.api.cache import DEFAULT_TTL_SECONDS, TTLCache
from aftermerge.api.connector import ClickHouseWarehouse, MetricResult, Warehouse
from aftermerge.telemetry import catalog

MAX_LOOKBACK_MINUTES = 7 * 24 * 60
MAX_LOOKBACK_DAYS = 90


def cache_key(name: str, params: dict[str, Any]) -> str:
    """Stable across parameter ordering, so the same question hits the same key."""
    rendered = ",".join(f"{k}={params[k]!r}" for k in sorted(params))
    return f"{name}({rendered})"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """One warehouse client for the process, opened once and closed on shutdown.

    A client per request would pay a connection handshake on every call, and a
    module-level client would connect at import time -- which breaks tests and
    makes the app unimportable without a reachable warehouse.
    """
    warehouse: Warehouse = getattr(app.state, "warehouse", None) or ClickHouseWarehouse()
    if isinstance(warehouse, ClickHouseWarehouse) and warehouse.client is None:
        await warehouse.connect()
    app.state.warehouse = warehouse
    try:
        yield
    finally:
        await warehouse.close()


def get_warehouse(request: Request) -> Warehouse:
    warehouse: Warehouse | None = getattr(request.app.state, "warehouse", None)
    if warehouse is None:  # pragma: no cover - lifespan always sets it
        raise HTTPException(status_code=503, detail="warehouse is not connected")
    return warehouse


def get_cache(request: Request) -> TTLCache[MetricResult]:
    cache: TTLCache[MetricResult] = request.app.state.cache
    return cache


#: `Annotated` rather than a `Depends()` default: the default-argument form
#: evaluates a call at definition time, which linters flag for good reason even
#: though FastAPI handles it.
WarehouseDep = Annotated[Warehouse, Depends(get_warehouse)]
CacheDep = Annotated[TTLCache[MetricResult], Depends(get_cache)]


async def _serve(
    name: str,
    params: dict[str, Any],
    warehouse: Warehouse,
    cache: TTLCache[MetricResult],
) -> dict[str, Any]:
    """Cache lookup, then the query, then the result with its provenance."""

    async def load() -> MetricResult:
        return await warehouse.run(name, **params)

    try:
        result, was_hit = await cache.get_or_load(cache_key(name, params), load)
    except Exception as exc:  # noqa: BLE001 - surfaced as 502, not a traceback
        raise HTTPException(status_code=502, detail=f"warehouse query failed: {exc}") from exc

    # `cached` reports this response, so the stored copy stays accurate for the
    # next caller rather than being mutated to say it was a hit.
    payload = result.as_dict()
    payload["cached"] = was_hit
    return payload


def create_app(
    *,
    warehouse: Warehouse | None = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> FastAPI:
    """Build the app. `warehouse` is injected by tests; production connects in lifespan."""
    app = FastAPI(
        title="AfterMerge metrics",
        summary="Read API over pre-aggregated telemetry rollups.",
        lifespan=lifespan,
    )
    if warehouse is not None:
        app.state.warehouse = warehouse
    app.state.cache = TTLCache[MetricResult](ttl_seconds=ttl_seconds)

    @app.get("/health")
    async def health(warehouse: WarehouseDep) -> dict[str, Any]:
        """Reports whether the warehouse answers, not merely whether this process is up.

        A health check that only proves the web server started is the kind that
        stays green through an outage.
        """
        reachable = await warehouse.ping()
        return {"status": "ok" if reachable else "degraded", "warehouse": reachable}

    @app.get("/catalog")
    async def catalog_index() -> dict[str, Any]:
        """The metric definitions, served from the same .sql files the queries run.

        Documentation that cannot drift: it is generated from the catalog rather
        than written alongside it.
        """
        return {
            "metrics": [
                {
                    "name": name,
                    "description": catalog.load(name).description,
                    "parameters": catalog.parameters(name),
                }
                for name in catalog.names()
            ]
        }

    @app.get("/metrics/latency")
    async def latency(
        warehouse: WarehouseDep,
        cache: CacheDep,
        service: str = Query("gateway"),
        route: str = Query("GET /orders"),
        lookback_minutes: int = Query(60, ge=1, le=MAX_LOOKBACK_MINUTES),
    ) -> dict[str, Any]:
        """Latency quantiles per deployed version."""
        return await _serve(
            "latency_quantiles",
            {"service": service, "route": route, "lookback_minutes": lookback_minutes},
            warehouse,
            cache,
        )

    @app.get("/metrics/latency/trend")
    async def latency_trend(
        warehouse: WarehouseDep,
        cache: CacheDep,
        service: str = Query("gateway"),
        route: str = Query("GET /orders"),
        lookback_days: int = Query(30, ge=1, le=MAX_LOOKBACK_DAYS),
    ) -> dict[str, Any]:
        """Day-over-day p95 per version: rolling mean, delta, and day index."""
        return await _serve(
            "route_latency_trend",
            {"service": service, "route": route, "lookback_days": lookback_days},
            warehouse,
            cache,
        )

    @app.get("/metrics/db-work")
    async def db_work(
        warehouse: WarehouseDep,
        cache: CacheDep,
        service: str = Query("orders"),
        lookback_minutes: int = Query(60, ge=1, le=MAX_LOOKBACK_MINUTES),
    ) -> dict[str, Any]:
        """Database spans per request, per version and code site."""
        return await _serve(
            "span_count_per_trace",
            {"service": service, "lookback_minutes": lookback_minutes},
            warehouse,
            cache,
        )

    @app.get("/cache")
    async def cache_stats(cache: CacheDep) -> dict[str, Any]:
        """Cache counters, including loads coalesced rather than duplicated."""
        return {"ttl_seconds": cache.ttl_seconds, "entries": cache.size, **cache.stats.as_dict()}

    return app
