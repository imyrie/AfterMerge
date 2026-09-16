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

ITEMS_FOR_ORDER_SQL = """
    SELECT order_id, sku, quantity, unit_price_cents
    FROM order_items
    WHERE order_id = $1
"""


async def list_orders(pool: asyncpg.Pool, limit: int) -> list[dict[str, Any]]:
    """Return recent orders with their line items.

    Fetches each order's items directly, which keeps the item lookup simple and
    avoids building an id array for the batch query.
    """
    async with pool.acquire() as conn:
        orders = await conn.fetch(ORDERS_SQL, limit)
        order_ids = [row["id"] for row in orders]

        grouped: dict[int, list[dict[str, Any]]] = {}
        for order_id in order_ids:
            items = await conn.fetch(ITEMS_FOR_ORDER_SQL, order_id)
            grouped[order_id] = [
                {
                    "sku": item["sku"],
                    "quantity": item["quantity"],
                    "unit_price_cents": item["unit_price_cents"],
                }
                for item in items
            ]

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
