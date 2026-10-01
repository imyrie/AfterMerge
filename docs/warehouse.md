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

## Loading

```bash
uv run aftermerge warehouse apply      # create the tables
uv run aftermerge warehouse refresh    # load only what changed
```

Creating the tables and filling them are separate commands, because they fail for
different reasons and a scheduler should be able to retry the second without re-running
the first.

### Incremental, by day partition

The loader reads a watermark -- the newest day already in the rollup -- and rebuilds only
the days at or after it. Measured on an 11-day benchmark table, a refresh after one new
day of spans arrives reads **33% of what a full rebuild reads**, and that share falls as
retention grows: the rebuild gets more expensive every day, the incremental window does not.

Three details carry most of the correctness:

**The watermark day is reloaded, not skipped.** Spans for a day keep arriving until the day
ends, so the newest rolled-up day was almost certainly incomplete when it was written.
Loading strictly *after* the watermark would leave every boundary day permanently short.

**A trailing window is reloaded anyway** (`--lookback-days`, default 2). A span can arrive
after its own day has been rolled up -- a delayed export, a collector restart, a backfilled
queue. Loading strictly forward would never see it, and the rollup would disagree with raw
forever in a way no query reveals.

**Each day is dropped before it is inserted.** `AggregatingMergeTree` merges rows with equal
keys rather than replacing them, so inserting over an existing day would *add* to it. Drop
then insert makes the load idempotent, which is what lets a scheduler retry a failed task
safely.

### Why there are no materialised views any more

The first version of this used materialised views for live updates plus a truncate-and-reload
backfill for history. That cannot be made idempotent: a view fires on insert, so a refresh
touching a day the view had already covered double counts it, and a truncate-then-reload has
a window where concurrent inserts are counted twice.

Partitions are daily rather than monthly for the same reason -- ClickHouse drops whole
partitions atomically, so monthly partitions would mean one late span rewrites the month.

Real-time reaction is the streaming consumer's job (see `docs/streaming.md`), which leaves
this table free to be a batch artifact that can be rebuilt on demand.

## Equivalence is checked before speed

`warehouse benchmark` runs both queries, normalises ordering and float noise, and compares the rows
before reporting any reduction. It exits non-zero if they disagree. A rollup that is fast and wrong
is worse than no rollup -- the same rule the patch validator follows, for the same reason.

## Reporting views over the rollup

`route_latency_trend.sql` answers "which way is it moving" rather than "are these two versions
different right now". Each day's value has to sit next to its neighbours instead of being aggregated
with them, so the daily buckets are built in a CTE and then read with window functions -- a rolling
3-day mean, the day-over-day delta, and a per-version day index.

Two details are easy to get wrong:

**`quantilesMerge` takes the same `(0.5, 0.95, 0.99)` the state was built with** and indexes p95 out
at `[2]`. Merging with a different parameter list than `quantilesState` used is an error, not a
reinterpretation.

**`lagInFrame(p95_ms, 1, NULL)`, not `lagInFrame(p95_ms)`.** The two-argument form returns the
column's *default* when there is no previous row, so on each version's first day `p95_ms - lag`
evaluated to `p95_ms - 0` and reported the entire latency as that day's change -- a 17-second jump
that never happened, on the one row where the honest answer is "unknown". The explicit `NULL`
default makes the subtraction `NULL`.

Reading the rollup rather than raw spans, the whole trend costs 18 rows read.
