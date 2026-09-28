-- fact: db_spans_per_request (rollup-backed)
-- Same answer as span_count_per_trace.sql, read from daily pre-aggregates.
--
-- uniqExactMerge combines the stored sketches, so request counts stay exact
-- across days rather than being summed and double counting shared traces.
--
-- The inner aliases are deliberately not the output names. Writing
-- `countMerge(db_spans) AS db_spans` shadows the column with its own alias, and
-- any later reference then resolves to the UInt64 result instead of the
-- AggregateFunction -- which fails with "Illegal type UInt64 of argument for
-- aggregate function with Merge suffix", a long way from the actual mistake.
SELECT
    version,
    code_site,
    spans                          AS db_spans,
    reqs                           AS requests,
    round(spans / reqs, 2)         AS spans_per_request
FROM
(
    SELECT
        version,
        code_site,
        countMerge(db_spans)   AS spans,
        uniqExactMerge(traces) AS reqs,
        min(day)               AS first_day
    FROM otel_db_work_rollup
    WHERE ServiceName = {service:String}
      AND day >= today() - {lookback_days:UInt32}
    GROUP BY version, code_site
)
ORDER BY first_day ASC
