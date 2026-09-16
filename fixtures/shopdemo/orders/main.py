"""shopdemo order service."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import asyncpg
from common import telemetry
from fastapi import FastAPI, Query

from orders import repository

telemetry.configure("orders")

DSN = os.environ.get("DATABASE_URL", "postgresql://aftermerge:aftermerge@postgres:5432/shopdemo")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DSN, min_size=2, max_size=10)
    yield
    await app.state.pool.close()


app = FastAPI(title="shopdemo-orders", lifespan=lifespan)
telemetry.instrument_app(app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": os.environ.get("GIT_SHA", "dev")}


@app.get("/orders")
async def list_orders(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
    orders = await repository.list_orders(app.state.pool, limit)
    return {"count": len(orders), "orders": orders}
