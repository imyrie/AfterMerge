-- Pre-aggregated rollups over the raw span table.
--
-- The catalog's hot queries all reduce raw spans to a handful of rows grouped by
-- deployed version. Doing that from raw means scanning every span every time,
-- and the scan grows with retention while the answer stays the same size.
--
-- AggregatingMergeTree stores partial aggregate *states*, so quantiles and
-- distinct counts stay correct when daily buckets are combined -- which a
-- pre-computed p95 per day could not do, since quantiles do not average.
--
-- The trade is time resolution: these buckets are daily, so rollup-backed
-- queries answer "which day" and not "which minute". Raw stays the source of
-- truth for narrow windows and for the detector's per-request samples.

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
PARTITION BY toYYYYMM(day)
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
PARTITION BY toYYYYMM(day)
ORDER BY (day, ServiceName, version, code_site);

-- Materialised views populate on insert, so they cover new spans only. Historical
-- partitions are filled by the backfill in warehouse/rollup.py.
CREATE MATERIALIZED VIEW IF NOT EXISTS otel_route_rollup_mv TO otel_route_rollup AS
SELECT
    toDate(Timestamp)                                   AS day,
    ServiceName,
    SpanName,
    ResourceAttributes['service.version']               AS version,
    countState()                                        AS requests,
    uniqExactState(TraceId)                             AS traces,
    quantilesState(0.5, 0.95, 0.99)(Duration / 1e6)     AS duration_quantiles,
    sumState(toUInt64(StatusCode = 'Error'))            AS errors
FROM otel_traces
WHERE SpanKind = 'Server'
GROUP BY day, ServiceName, SpanName, version;

CREATE MATERIALIZED VIEW IF NOT EXISTS otel_db_work_rollup_mv TO otel_db_work_rollup AS
SELECT
    toDate(Timestamp)                     AS day,
    ServiceName,
    ResourceAttributes['service.version'] AS version,
    SpanAttributes['code.file.path']      AS code_site,
    countState()                          AS db_spans,
    uniqExactState(TraceId)               AS traces
FROM otel_traces
WHERE SpanKind = 'Client' AND SpanAttributes['code.file.path'] != ''
GROUP BY day, ServiceName, version, code_site;
