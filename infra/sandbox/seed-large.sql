-- Deterministic seed at production scale, for volume-dependent regressions.
--
-- The default seed is small so sandboxes start quickly, which makes it unable to
-- reproduce a fault whose severity depends on table size: an index-defeating
-- cast costs almost nothing over 8,000 rows and 160x over hundreds of thousands.
--
-- Same determinism contract as the small seed -- setseed() and a fixed epoch --
-- so both sides of a differential still see byte-identical data. The only thing
-- that changes is how much of it there is.

SELECT setseed(0.42);

INSERT INTO customers (name, email)
SELECT 'Customer ' || i, 'customer' || i || '@example.test'
FROM generate_series(1, 200) AS i;

INSERT INTO orders (customer_id, status, total_cents, created_at)
SELECT
    1 + (random() * 199)::int,
    (ARRAY['pending','paid','shipped','delivered'])[1 + (random() * 3)::int],
    (500 + random() * 45000)::bigint,
    TIMESTAMPTZ '2026-01-01 00:00:00+00' - (random() * 30 || ' days')::interval
FROM generate_series(1, 25000);

INSERT INTO order_items (order_id, sku, quantity, unit_price_cents)
SELECT
    o.id,
    'SKU-' || lpad(((random() * 9999)::int)::text, 4, '0'),
    1 + (random() * 3)::int,
    (300 + random() * 12000)::bigint
FROM orders o, generate_series(1, 16);

ANALYZE;
