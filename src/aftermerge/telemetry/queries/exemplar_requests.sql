-- fact: exemplar_requests
-- Distinct request shapes served by one version, with a representative trace each.
--
-- Grouped rather than listed: 3,586 identical `GET /orders?limit=50` calls are
-- one shape, and storing fifty copies of it would be noise. `argMax(TraceId,
-- Duration)` keeps the slowest example of each shape, which is the one most
-- likely to exhibit whatever went wrong.
--
-- Both `http.url` and `http.target` are selected because they disagree: the
-- FastAPI instrumentation records `http.target` WITHOUT the query string, so
-- `http.url` is the only attribute that preserves it.
SELECT
    SpanAttributes['http.method']                     AS method,
    SpanAttributes['http.url']                        AS url,
    SpanAttributes['http.target']                     AS target,
    SpanAttributes['http.route']                      AS route,
    SpanAttributes['http.user_agent']                 AS user_agent,
    toUInt16OrZero(SpanAttributes['http.status_code']) AS status_code,
    count()                                           AS observations,
    argMax(TraceId, Duration)                         AS exemplar_trace_id,
    round(max(Duration) / 1e6, 1)                     AS max_ms
FROM otel_traces
WHERE ServiceName = {service:String}
  AND SpanKind = 'Server'
  AND ResourceAttributes['service.version'] = {version:String}
  AND Timestamp >= now() - INTERVAL {lookback_minutes:UInt32} MINUTE
  AND SpanAttributes['http.route'] NOT IN ('/health', '/healthz', '/metrics')
GROUP BY method, url, target, route, user_agent, status_code
ORDER BY max_ms DESC
LIMIT {max_shapes:UInt32}
