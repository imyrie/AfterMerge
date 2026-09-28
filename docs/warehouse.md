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

## Measured, 2026-09-28

57,732 raw spans. Best of three runs, and equivalence checked before any speedup is reported.

| question | rows read | bytes read | elapsed | agrees |
|---|---|---|---|---|
| route latency | 8,198 → 9 | 708 KB → 1.6 KB | 29ms → 21ms | yes |
| db work per request | 57,726 → 4 | 23.6 MB → 1.1 KB | 78ms → 10ms | yes |

**Bytes read is the honest headline.** At this data volume elapsed time is dominated by fixed
per-query overhead, so the 8x is understated; the 22,000x reduction in bytes scanned is what actually
changes as the table grows.

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
