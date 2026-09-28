-- fact: route_latency_quantiles (rollup-backed)
-- Same answer as latency_quantiles.sql, read from daily pre-aggregates.
--
-- Column names match the raw query exactly, so anything consuming one consumes
-- the other unchanged.
--
-- quantilesMerge combines the stored *states*, which is why daily buckets can be
-- summed into a correct overall p95. Averaging per-day p95 values would not be
-- correct, and is the usual way a rollup like this goes quietly wrong.
SELECT
    version,
    countMerge(requests)                                          AS requests,
    round(quantilesMerge(0.5, 0.95, 0.99)(duration_quantiles)[1], 1) AS p50_ms,
    round(quantilesMerge(0.5, 0.95, 0.99)(duration_quantiles)[2], 1) AS p95_ms,
    round(quantilesMerge(0.5, 0.95, 0.99)(duration_quantiles)[3], 1) AS p99_ms,
    sumMerge(errors)                                              AS errors
FROM otel_route_rollup
WHERE ServiceName = {service:String}
  AND SpanName = {route:String}
  AND day >= today() - {lookback_days:UInt32}
GROUP BY version
ORDER BY min(day) ASC
