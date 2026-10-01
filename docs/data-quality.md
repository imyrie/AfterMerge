# Data quality

`aftermerge dq` checks the data before anything draws conclusions from it. It exits 0 when the data
is trustworthy, 1 when a **blocking** check fails, and 2 when no check could produce evidence.

```bash
uv run aftermerge dq
```

## Why this exists

`latency_quantiles.sql` counted `StatusCode = 'STATUS_CODE_ERROR'`. The ClickHouse exporter writes
the short form, `'Error'`, so the predicate matched nothing and **the error column read zero for
every measurement in every slice**.

Nothing failed. No exception, no empty result, no warning. The number was simply wrong and looked
entirely plausible -- zero is a perfectly reasonable error count. It survived three slices and was
only found when a scenario produced 62% failures and the zero became impossible to believe.

That is the shape of defect these checks exist for. A pipeline that only notices problems when
something crashes will report confident, wrong numbers indefinitely.

## The checks

| check | what it catches |
|---|---|
| `filter_literals_match_data` | a query filtering on a literal the exporter cannot emit -- returns zero rather than failing |
| `status_code_domain` | the exporter's vocabulary moving, so existing filters silently stop matching |
| `freshness` | analysis describing a past the deploy has already left |
| `version_cardinality` | a window holding only one version, where no before/after comparison is possible |
| `server_span_completeness` | missing `http.url`, which makes request capture replay the wrong target |
| `referential_integrity` | orphaned rows, including a reference the schema cannot enforce |

### The first one is the direct control

It reads every literal the SQL catalog compares against a watched column, and classifies it against
the vocabulary the exporter can emit. Run against the original defect:

```
StatusCode='STATUS_CODE_ERROR' is not a value the exporter emits
  (vocabulary: ['Error', 'Ok', 'Unset']); the filter can never match
```

A regex over the `.sql` files rather than a parser, deliberately: the catalog is small, hand-written
and stable, and a typo in a literal is exactly what a regex sees perfectly well.

## Blocking versus advisory

Two findings look identical from the query's side -- the metric returns zero -- but mean different
things, and the check tells them apart:

- **Impossible.** The literal is not in the column's vocabulary, so no data will ever satisfy it.
  `'STATUS_CODE_ERROR'` is this. The metric is broken; the run **blocks**.
- **Absent.** The literal is valid but does not occur in the current window. `StatusCode='Error'`
  is this during a latency-only regression, where there genuinely are no error spans. The data and
  the query are both sound and one metric reads zero for a legitimate reason, so the finding is
  **advisory**: it prints, and the exit code stays 0.

```
filter_literals_match_data  advisory  StatusCode='Error' does not occur in the
                                      current data (observed: ['Unset']);
                                      that metric reads zero
```

Collapsing the two would mean either losing the original defect or refusing to analyse any window
that happens to be free of errors -- which is most of them. The classification is driven by the
declared vocabulary rather than by what was observed, so an impossible literal keeps blocking even
in a window that does contain errors.

`severity` defaults to `BLOCKING`, so a check has to opt out deliberately rather than by omission.

### The last one covers a gap Postgres cannot

`hypotheses.supporting_fact_ids` is a uuid array. The schema enforces that it is non-empty, but not
that the ids resolve -- a hypothesis citing a deleted fact satisfies every constraint while citing
nothing. Foreign keys cannot express that, so a check does.

## Skips are not passes

A check that could not run reports `skipped`, and a report containing only skips does **not** pass.
Absent evidence is not positive evidence -- the same rule the patch validator follows.
