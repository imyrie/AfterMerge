-- fact: route_latency_trend
-- Day-over-day p95 latency per deployed version, read from the daily rollup.
--
-- The other latency queries answer "are these two versions different right
-- now". This one answers "which way is it moving", which needs each day's value
-- placed next to its neighbours rather than aggregated with them -- so the
-- daily buckets are computed in a CTE and then read with window functions.
--
-- quantilesMerge takes the same (0.5, 0.95, 0.99) the state was built with and
-- indexes p95 out at [2]. Merging with a different parameter list than
-- quantilesState used is an error, not a reinterpretation.
--
-- lagInFrame(p95_ms, 1, NULL) rather than lagInFrame(p95_ms): the two-argument
-- form returns the column's *default* when no previous row exists, so on each
-- version's first day `p95_ms - lag` evaluated to p95_ms - 0 and reported the
-- whole latency as that day's change. A 17-second jump that never happened, on
-- a row where the honest answer is "unknown". The explicit NULL default makes
-- the subtraction NULL instead.
WITH daily AS (
    SELECT
        version,
        day,
        countMerge(requests)                                             AS requests,
        round(quantilesMerge(0.5, 0.95, 0.99)(duration_quantiles)[2], 1) AS p95_ms
    FROM otel_route_rollup
    WHERE ServiceName = {service:String}
      AND SpanName = {route:String}
      AND day >= today() - {lookback_days:UInt32}
    GROUP BY version, day
)
SELECT
    version,
    day,
    requests,
    p95_ms,
    round(avg(p95_ms) OVER rolling, 1)                                   AS p95_rolling_3d,
    round(p95_ms - lagInFrame(toNullable(p95_ms), 1, NULL) OVER prev, 1) AS p95_delta,
    row_number() OVER prev                                               AS day_index
FROM daily
WINDOW
    rolling AS (PARTITION BY version ORDER BY day ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
    prev    AS (PARTITION BY version ORDER BY day ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
ORDER BY version ASC, day ASC
