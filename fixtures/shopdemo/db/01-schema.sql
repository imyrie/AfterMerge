-- shopdemo: the system AfterMerge observes. Deliberately small.

CREATE TABLE customers (
    id    BIGSERIAL PRIMARY KEY,
    name  TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE
);

CREATE TABLE orders (
    id          BIGSERIAL PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    status      TEXT   NOT NULL,
    total_cents BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    id               BIGSERIAL PRIMARY KEY,
    order_id         BIGINT NOT NULL REFERENCES orders(id),
    sku              TEXT   NOT NULL,
    quantity         INT    NOT NULL,
    unit_price_cents BIGINT NOT NULL
);

-- Present deliberately. The N+1 regression must be slow because of round-trip
-- count, not because of a missing index -- otherwise the demo proves the wrong thing.
CREATE INDEX idx_order_items_order_id ON order_items(order_id);
CREATE INDEX idx_orders_created_at    ON orders(created_at DESC);
