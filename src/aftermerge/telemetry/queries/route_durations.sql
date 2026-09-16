-- fact: route_duration_samples
-- Raw per-request durations for one route, for two specific versions.
--
-- Returns individual samples rather than pre-aggregated quantiles because the
-- detector runs a distribution-free significance test (Mann-Whitney U), which
-- needs the observations themselves. `LIMIT n BY version` caps each side so one
-- long-running window cannot dominate the comparison.
SELECT
    ResourceAttributes['service.version'] AS version,
    Duration / 1e6                        AS duration_ms
FROM otel_traces
WHERE ServiceName = {service:String}
  AND SpanKind = 'Server'
  AND SpanName = {route:String}
  AND ResourceAttributes['service.version'] IN ({baseline:String}, {candidate:String})
  AND Timestamp >= now() - INTERVAL {lookback_minutes:UInt32} MINUTE
ORDER BY Timestamp ASC
LIMIT {max_samples:UInt32} BY version
