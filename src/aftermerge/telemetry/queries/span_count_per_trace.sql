-- fact: db_spans_per_request
-- Application-attributed database spans per request, split by deployed version.
--
-- This is slice 0's headline signal. An N+1 is invisible in latency alone (it
-- looks like "the database got slower") but unmistakable here.
--
-- code.file.path != '' excludes driver-internal work such as asyncpg's
-- connection-pool reset, which no application frame is responsible for.
SELECT
    ResourceAttributes['service.version']    AS version,
    SpanAttributes['code.file.path']         AS code_site,
    count()                                  AS db_spans,
    uniqExact(TraceId)                       AS requests,
    round(count() / uniqExact(TraceId), 2)   AS spans_per_request
FROM otel_traces
WHERE ServiceName = {service:String}
  AND SpanKind = 'Client'
  AND SpanAttributes['code.file.path'] != ''
  AND Timestamp >= now() - INTERVAL {lookback_minutes:UInt32} MINUTE
GROUP BY version, code_site
ORDER BY min(Timestamp) ASC
