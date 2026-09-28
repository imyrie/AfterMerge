# Warehouse rollups

The catalog's hot queries reduce raw spans to a handful of rows grouped by deployed version. Doing
that from raw scans every span every time, and the scan grows with retention while the answer stays
the same size.

```bash
uv run aftermerge warehouse apply      # create rollups + backfill history
uv run aftermerge warehouse benchmark  # check equivalence, then report cost
```

## The model

| table | grain | holds |
|---|---|---|
| `otel_traces` | one row per span | source of truth |
| `otel_route_rollup` | day x service x route x version | request count, distinct traces, duration quantiles, errors |
| `otel_db_work_rollup` | day x service x version x code site | database spans, distinct traces |

`AggregatingMergeTree` stores partial aggregate **states**, not finished numbers. That is what makes
daily buckets combinable: `quantilesMerge` reconstructs a correct overall p95, whereas averaging
per-day p95 values would be wrong. `uniqExactMerge` likewise keeps distinct trace counts exact
instead of summing and double counting traces that span a day boundary.

## Measured

### At retention scale

392,457 spans over 10 days. Best of three runs, equivalence checked before any speedup is reported.

| question | rows read | bytes read | elapsed |
|---|---|---|---|
| route latency | 32,870 -> 41 (802x) | 5.8 MB -> 6.9 KB (840x) | 74.5ms -> 5.9ms (12.7x) |
| db work per request | 367,779 -> 17 (21,634x) | 159.9 MB -> 4.3 KB (37,418x) | 101.3ms -> 7.9ms (12.8x) |

### On a single afternoon of real traffic

57,732 spans over 2 days, for comparison:

| question | rows read | bytes read | elapsed |
|---|---|---|---|
| route latency | 8,198 -> 9 | 708 KB -> 1.6 KB | 29ms -> 21ms |
| db work per request | 57,726 -> 4 | 23.6 MB -> 1.1 KB | 78ms -> 10ms |

**The comparison between those two tables is the actual point.** The rollup's answer stayed the same
size while the raw scan grew with retention, so the gap widens with every day kept. Elapsed time at
the smaller volume is mostly fixed per-query overhead, which is why the 2-day speedup understates the
technique; bytes read is the honest measure at any scale.

### How the retention-scale data was produced

`scripts/generate_benchmark_data.py` clones real spans across a span of days, giving each copy a
fresh trace id so distinct-count aggregates stay meaningful. It is **a retention simulation, not
recorded traffic** -- same schema, same attribute payloads, same distribution, more history than a
laptop accumulates in an afternoon. It writes to a separate database, so the `otel` data the pipeline
uses is never touched.

The target was 30 days; ClickHouse stopped at 10. Reading the wide `SpanAttributes` map repeatedly
pushes its memory tracker past the container's 3.44 GiB ceiling, and capping block size and thread
count did not bring it far enough down. The resulting numbers are from what fits on this machine, and
they would keep improving with more history rather than plateauing.

## What it costs

**Time resolution.** Buckets are daily, so rollup-backed queries answer "which day", not "which
minute". Raw remains the source of truth for narrow windows and for the detector's per-request
duration samples, which cannot come from an aggregate at all.

**Backfill is not automatic.** Materialised views only see new inserts, so `warehouse apply`
rebuilds history explicitly. It truncates first, because `AggregatingMergeTree` combines rows with
equal keys rather than replacing them -- re-inserting over existing buckets would double count. That
assumes ingest is quiet: a span arriving between the truncate and the insert is counted twice.

## Equivalence is checked before speed

`warehouse benchmark` runs both queries, normalises ordering and float noise, and compares the rows
before reporting any reduction. It exits non-zero if they disagree. A rollup that is fast and wrong
is worse than no rollup -- the same rule the patch validator follows, for the same reason.
