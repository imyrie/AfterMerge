-- Pre-aggregated rollups over the raw span table.
--
-- The catalog's hot queries reduce raw spans to a handful of rows grouped by
-- deployed version. From raw that scans every span every time, and the scan
-- grows with retention while the answer stays the same size.
--
-- AggregatingMergeTree stores partial aggregate *states*, so quantiles and
-- distinct counts stay correct when daily buckets are combined -- which a
-- pre-computed p95 per day could not do, since quantiles do not average.
--
-- PARTITION BY day, not by month. A day is the unit the loader reloads, and
-- ClickHouse can only drop whole partitions atomically. Monthly partitions
-- would mean reloading a single late-arriving span rewrites the entire month.

CREATE TABLE IF NOT EXISTS otel_route_rollup
(
    day                Date,
    ServiceName        LowCardinality(String),
    SpanName           LowCardinality(String),
    version            LowCardinality(String),
    requests           AggregateFunction(count),
    traces             AggregateFunction(uniqExact, String),
    duration_quantiles AggregateFunction(quantiles(0.5, 0.95, 0.99), Float64),
    errors             AggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree
PARTITION BY day
ORDER BY (day, ServiceName, SpanName, version);

CREATE TABLE IF NOT EXISTS otel_db_work_rollup
(
    day         Date,
    ServiceName LowCardinality(String),
    version     LowCardinality(String),
    code_site   String,
    db_spans    AggregateFunction(count),
    traces      AggregateFunction(uniqExact, String)
)
ENGINE = AggregatingMergeTree
PARTITION BY day
ORDER BY (day, ServiceName, version, code_site);

-- There are deliberately no materialised views here any more.
--
-- A view that fires on insert cannot be re-run: AggregatingMergeTree combines
-- rows with equal keys rather than replacing them, so a refresh that touched a
-- day the view had already covered would double count it. That makes the load
-- non-idempotent, and a non-idempotent step cannot be safely retried -- which is
-- the first thing any scheduler does when a task fails.
--
-- Loads are therefore drop-partition-then-insert, which produces the same result
-- however many times it runs. Real-time reaction is the streaming consumer's job
-- (see docs/streaming.md), not this table's.
