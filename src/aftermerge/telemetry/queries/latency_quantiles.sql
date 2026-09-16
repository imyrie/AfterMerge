-- fact: route_latency_quantiles
-- Server-span latency distribution for one route, split by deployed version.
--
-- Read from the SERVER span, which is what a user actually waits for --
-- not the client spans, which measure only the pieces.
SELECT
    ResourceAttributes['service.version']              AS version,
    count()                                            AS requests,
    round(quantile(0.50)(Duration) / 1e6, 1)           AS p50_ms,
    round(quantile(0.95)(Duration) / 1e6, 1)           AS p95_ms,
    round(quantile(0.99)(Duration) / 1e6, 1)           AS p99_ms,
    countIf(StatusCode = 'STATUS_CODE_ERROR')          AS errors
FROM otel_traces
WHERE ServiceName = {service:String}
  AND SpanKind = 'Server'
  AND SpanName = {route:String}
  AND Timestamp >= now() - INTERVAL {lookback_minutes:UInt32} MINUTE
GROUP BY version
ORDER BY min(Timestamp) ASC
