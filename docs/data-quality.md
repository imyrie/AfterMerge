# Data quality

`aftermerge dq` checks the data before anything draws conclusions from it. It exits 0 when the data
is trustworthy, 1 when a check fails, and 2 when no check could produce evidence.

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
| `filter_literals_match_data` | a query filtering on a literal that never occurs -- returns zero rather than failing |
| `status_code_domain` | the exporter's vocabulary moving, so existing filters silently stop matching |
| `freshness` | analysis describing a past the deploy has already left |
| `version_cardinality` | a window holding only one version, where no before/after comparison is possible |
| `server_span_completeness` | missing `http.url`, which makes request capture replay the wrong target |
| `referential_integrity` | orphaned rows, including a reference the schema cannot enforce |

### The first one is the direct control

It reads every literal the SQL catalog compares against a watched column, and asserts each one
actually occurs in the data. Run against the original defect it reports:

```
StatusCode='STATUS_CODE_ERROR' never occurs (observed: ['Error', 'Unset'])
  -- such a filter returns zero rather than failing
```

A regex over the `.sql` files rather than a parser, deliberately: the catalog is small, hand-written
and stable, and a typo in a literal is exactly what a regex sees perfectly well.

### The last one covers a gap Postgres cannot

`hypotheses.supporting_fact_ids` is a uuid array. The schema enforces that it is non-empty, but not
that the ids resolve -- a hypothesis citing a deleted fact satisfies every constraint while citing
nothing. Foreign keys cannot express that, so a check does.

## Skips are not passes

A check that could not run reports `skipped`, and a report containing only skips does **not** pass.
Absent evidence is not positive evidence -- the same rule the patch validator follows.
