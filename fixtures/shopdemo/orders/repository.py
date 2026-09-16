"""Order data access.

This module is the target of slice 0's regression. Keep it small so the diff
between the good and bad commits is unambiguous.
"""

from __future__ import annotations

from typing import Any

import asyncpg

ORDERS_SQL = """
    SELECT id, customer_id, status, total_cents, created_at
    FROM orders
    ORDER BY created_at DESC
    LIMIT $1
"""

ITEMS_BATCH_SQL = """
    SELECT order_id, sku, quantity, unit_price_cents
    FROM order_items
    WHERE order_id = ANY($1::bigint[])
"""


async def list_orders(pool: asyncpg.Pool, limit: int) -> list[dict[str, Any]]:
    """Return recent orders with their line items.

    Two queries regardless of page size: one for the orders, one batched fetch
    for every line item belonging to them.
    """
    async with pool.acquire() as conn:
        orders = await conn.fetch(ORDERS_SQL, limit)
        order_ids = [row["id"] for row in orders]

        items = await conn.fetch(ITEMS_BATCH_SQL, order_ids) if order_ids else []

    grouped: dict[int, list[dict[str, Any]]] = {oid: [] for oid in order_ids}
    for item in items:
        grouped[item["order_id"]].append(
            {
                "sku": item["sku"],
                "quantity": item["quantity"],
                "unit_price_cents": item["unit_price_cents"],
            }
        )

    return [
        {
            "id": o["id"],
            "customer_id": o["customer_id"],
            "status": o["status"],
            "total_cents": o["total_cents"],
            "created_at": o["created_at"].isoformat(),
            "items": grouped[o["id"]],
        }
        for o in orders
    ]
