-- fact: dependency_attribution
-- Where a request's time is spent, split by deployed version.
--
-- Answers "which dependency got slower" rather than "the service got slower".
-- Reports total time as well as per-request time: an N+1 shows as a modest
-- per-call time alongside a large call count, which is the shape that
-- distinguishes "each query is slow" from "there are far more queries".
SELECT
    ResourceAttributes['service.version']              AS version,
    SpanName                                           AS operation,
    count()                                            AS calls,
    round(count() / uniqExact(TraceId), 2)             AS calls_per_request,
    round(avg(Duration) / 1e6, 3)                      AS avg_ms,
    round(sum(Duration) / uniqExact(TraceId) / 1e6, 2) AS total_ms_per_request
FROM otel_traces
WHERE ServiceName = {service:String}
  AND SpanKind = 'Client'
  AND Timestamp >= now() - INTERVAL {lookback_minutes:UInt32} MINUTE
GROUP BY version, operation
ORDER BY version, total_ms_per_request DESC
