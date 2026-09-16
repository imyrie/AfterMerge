-- Enough rows that an N+1 over a 50-row page is unmistakable.

INSERT INTO customers (name, email)
SELECT 'Customer ' || i, 'customer' || i || '@example.test'
FROM generate_series(1, 200) AS i;

INSERT INTO orders (customer_id, status, total_cents, created_at)
SELECT
    1 + (random() * 199)::int,
    (ARRAY['pending','paid','shipped','delivered'])[1 + (random() * 3)::int],
    (500 + random() * 45000)::bigint,
    now() - (random() * 30 || ' days')::interval
FROM generate_series(1, 2000);

INSERT INTO order_items (order_id, sku, quantity, unit_price_cents)
SELECT
    o.id,
    'SKU-' || lpad(((random() * 9999)::int)::text, 4, '0'),
    1 + (random() * 3)::int,
    (300 + random() * 12000)::bigint
FROM orders o, generate_series(1, 4);

ANALYZE;
