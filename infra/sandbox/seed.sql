-- Deterministic seed, owned by the reproducer rather than by the commit under test.
--
-- The fixture's own 02-seed.sql calls random() unseeded, so two sandboxes built
-- from two different commits get two different datasets -- and a differential
-- comparison across them is then comparing two databases, not two code paths.
--
-- setseed() makes every subsequent random() in this session reproducible, so
-- both sides of a replay see byte-identical data.

SELECT setseed(0.42);

INSERT INTO customers (name, email)
SELECT 'Customer ' || i, 'customer' || i || '@example.test'
FROM generate_series(1, 200) AS i;

INSERT INTO orders (customer_id, status, total_cents, created_at)
SELECT
    1 + (random() * 199)::int,
    (ARRAY['pending','paid','shipped','delivered'])[1 + (random() * 3)::int],
    (500 + random() * 45000)::bigint,
    -- Fixed epoch, not now(): otherwise the ORDER BY created_at ordering shifts
    -- between a sandbox built today and one built tomorrow.
    TIMESTAMPTZ '2026-01-01 00:00:00+00' - (random() * 30 || ' days')::interval
FROM generate_series(1, 2000);

INSERT INTO order_items (order_id, sku, quantity, unit_price_cents)
SELECT
    o.id,
    'SKU-' || lpad(((random() * 9999)::int)::text, 4, '0'),
    1 + (random() * 3)::int,
    (300 + random() * 12000)::bigint
FROM orders o, generate_series(1, 4);

ANALYZE;
