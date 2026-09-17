# Scenario 002 — an index-defeating cast

**Purpose:** test whether the architecture generalises, or was fitted to the N+1 it was built
alongside. This regression has the **inverse signal profile**: latency rises sharply while the number
of database operations per request does not move at all.

| | scenario 001 (N+1) | scenario 002 (cast) |
|---|---|---|
| db operations per request | 2 → 51 | 2 → 2 |
| p95 latency | 156ms → 1753ms | 1242ms → 61890ms |
| error rate | 0% → 0% | 0% → 62% |
| what the detector sees | work amplification | latency and errors |

---

## What the scenario is, and what it was meant to be

It was meant to be a **dropped index**. Three measurements killed that:

1. **At 8,000 rows the index was counterproductive.** Dropping it made the query 24x *faster*
   (83.6ms → 3.5ms): on a small table a sequential scan beats an index lookup. The original fixture
   is sized for regressions that multiply round trips, not ones that multiply scan cost.
2. **At 800,000 rows it bites properly** — 25ms indexed against 166ms scanned. So the shape is real,
   it just needs about 100x the data. The fixture dataset was scaled to 50,000 orders and 800,000
   items.
3. **A dropped index is not deploy-shaped in this fixture.** The index is created by volume-init SQL
   and there is no migration mechanism, so no deploy can introduce it. More importantly, AfterMerge's
   detector compares *deployed versions* -- a DBA dropping an index in production is structurally
   invisible to it. That is a genuine limitation of the design, not of the fixture.

What was built instead has the same signal profile and *is* deploy-shaped: comparing order ids as
text (`order_id::text = ANY($1::text[])`) defeats the index on `order_items(order_id)`. Same two
queries, one of them now a parallel sequential scan. Measured: **1.4ms indexed, 228ms scanned.**

Under load at 20 rps the service collapsed -- p50 30.7s, 62% of requests failing -- which is more
severe than intended and made the error-rate findings below unmissable.

---

## What it confirmed

All three predicted honest-failure behaviours held:

| stage | behaviour |
|---|---|
| **Detection** | fires on latency and errors, with `db spans/req 2.0 -> 2.0 (1.0x)` |
| **Correlation** | `temporal_correlation`, score 0.35 -- no new work exists for the diff to explain |
| **Test generation** | declines, exit 2: "the evidence cannot support a discriminating test" |

The detector's two-signal design earned its keep in the opposite direction from slice 1. There, work
amplification caught what latency missed; here, latency catches what work amplification cannot see.

---

## Five bugs it exposed

Every one of these was invisible to scenario 001, and four of them were silent.

### 1. Correlation overclaimed on measurement noise

The candidate measured **2.04** operations per request against a baseline of **2.00** -- four
hundredths, from traces that flushed mid-window. That was enough to take the mechanical branch, score
`0.04 / 0.04` as a perfect match, and report:

> **explains 100% of the new work** -- Commit b4726eb ... accounts for **0 of the 0** additional
> database operations per request.

Full confidence, from noise, on a regression that added no work whatsoever. Fixed with a
`MATERIAL_WORK_DELTA` floor of one whole operation per request: half an operation is not an operation.

### 2. Re-running an investigation stacked contradictory hypotheses

`investigate` proposed a *new* hypothesis every run rather than refreshing the existing one. After
the fix above, the incident carried both the stale score-1.00 claim and the correct score-0.35 one --
and since hypotheses render by score descending, **the stale wrong one was the only thing visible**.

Fixed with `HypothesisRepository.supersede`, which updates in place rather than deleting and
re-inserting: verifications reference a hypothesis by id, so deleting one would cascade and destroy
level-3 evidence.

### 3. The score label assumed a basis it no longer had

With the above fixed, the report still read **"explains 35% of the new work"** for a timing-only
correlation -- describing work that does not exist. The stored hypothesis had no field distinguishing
the two bases, so the renderer could not tell them apart. The basis now becomes the `kind`
(`change_correlation` / `temporal_correlation`), and a timing claim renders as
**"timing correlation only (score 0.35)"**.

### 4. The error-rate column had always been zero

`latency_quantiles.sql` counted `StatusCode = 'STATUS_CODE_ERROR'`. The ClickHouse exporter writes
the short form, **`'Error'`**, so the predicate matched nothing. The column read 0 while 62% of
requests were failing -- and had read 0 for every measurement in every earlier slice.

### 5. `SLO.max_error_rate` was declared and never read

The field existed from slice 1 and no rule consulted it. A regression failing 62% of its requests
could only ever be caught by its latency. Errors are now a first-class trigger, and they do not
require the latency sample floor -- a service that is failing does not need a significance test:

```
critical regression detected
  - 62.1% of requests are failing, against an objective of 1.0% (baseline 0.0%)
  - candidate is slower (Mann-Whitney p=5.32e-293 < 0.01)
  - p95 rose 49.8x (1242ms -> 61889ms)
```

---

## One limitation worth recording

The reproducer seeds sandboxes from a fixed, deliberately small dataset, so that both sides of a
differential see identical data. That makes it **structurally unable to reproduce a regression whose
severity depends on data volume**: replayed against 8,000 rows, this cast costs almost nothing.

Slice 2's determinism guarantee and the ability to reproduce volume-dependent faults are in direct
tension. Resolving it means per-scenario seeds, which is a real change to the sandbox contract rather
than a tweak.
