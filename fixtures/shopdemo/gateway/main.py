"""shopdemo gateway: the public entry point, fronting the order service.

Exists so traces span two services. A single-service demo cannot show
cross-service context propagation, which slice 1's trace analysis depends on.
"""

from __future__ import annotations

import os

import httpx
from common import telemetry
from fastapi import FastAPI, HTTPException, Query

telemetry.configure("gateway")

ORDERS_URL = os.environ.get("ORDERS_URL", "http://orders:8000")

app = FastAPI(title="shopdemo-gateway")
telemetry.instrument_app(app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": os.environ.get("GIT_SHA", "dev")}


@app.get("/orders")
async def orders(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{ORDERS_URL}/orders", params={"limit": limit})
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="order service unavailable")
    return response.json()
