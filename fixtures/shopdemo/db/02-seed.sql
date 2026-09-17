-- Enough rows that BOTH regression shapes are measurable.
--
-- Sized deliberately. Scenario 001 (N+1) shows up at any volume, because it
-- multiplies round trips rather than scan cost. Scenario 002 (an index-defeating
-- cast) is invisible below roughly 100k rows: on the original 8,000-row table a
-- sequential scan actually beat the index, so dropping or defeating it made the
-- query *faster*. Measured at 800k rows: 25ms indexed, 166ms scanned.

INSERT INTO customers (name, email)
SELECT 'Customer ' || i, 'customer' || i || '@example.test'
FROM generate_series(1, 200) AS i;

INSERT INTO orders (customer_id, status, total_cents, created_at)
SELECT
    1 + (random() * 199)::int,
    (ARRAY['pending','paid','shipped','delivered'])[1 + (random() * 3)::int],
    (500 + random() * 45000)::bigint,
    now() - (random() * 30 || ' days')::interval
FROM generate_series(1, 50000);

INSERT INTO order_items (order_id, sku, quantity, unit_price_cents)
SELECT
    o.id,
    'SKU-' || lpad(((random() * 9999)::int)::text, 4, '0'),
    1 + (random() * 3)::int,
    (300 + random() * 12000)::bigint
FROM orders o, generate_series(1, 16);

ANALYZE;
