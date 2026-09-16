# Slice 1 — checklist

**Goal:** find the regression from telemetry alone, and record every conclusion with the
evidence that supports it.

| Part | Scope | State |
|---|---|---|
| 1 | Store: audit trail + trust-level invariants | **done** |
| 2 | Detector: windows, statistics, rules | **done** |
| 3 | Evidence gathering into facts | partial — detector records 3 facts |
| 4 | Change correlation → ranked hypotheses | not started |
| 5 | Report + `aftermerge investigate` + e2e assertions | not started |

---

## Part 1 — The store

The three trust levels are enforced by the schema and the repositories, not by convention:

| Level | Table | Write path demands |
|---|---|---|
| 1 observed | `facts` | a `FactResult` from an executed query — the statement and parameters are stored alongside the value |
| 2 inferred | `hypotheses` | at least one supporting fact id, enforced in Python *and* by a `cardinality(...) > 0` check constraint |
| 3 verified | `verifications` | a `subprocess.CompletedProcess`; the verdict is derived from `returncode` and cannot be supplied by the caller |

Level 3 is the load-bearing one. A language model can produce a convincing string but not a
`CompletedProcess`, so there is no code path by which it can assert that something was verified.
`test_a_failing_process_cannot_be_recorded_as_confirmed` and
`test_verification_requires_a_real_process_object` hold that line.

---

## Part 2 — The detector

**Windows are defined by deployed version, not wall-clock time.** Comparing "the last 10 minutes
against the 30 before" breaks whenever deploys overlap, roll out gradually, or get rolled back —
all of which put two versions in one window and silently average them. Splitting on
`service.version` is correct in every one of those cases.

`NoComparisonAvailable` is raised — never a false all-clear — when there are no deploys, no
recorded predecessor, or the last deploy redeployed the same commit.

**Two signals, either sufficient:**

1. **Latency** — Mann-Whitney U (latency distributions are heavily right-skewed, so a t-test would
   answer a different question) plus rank-biserial effect size. Requires significance **and** a
   material ratio: at high request volume a 3% slowdown is statistically significant and
   operationally irrelevant, and paging on it is the noise failure this project exists to avoid.
2. **Work amplification** — database spans per request, baseline vs candidate.

Below the sample floor the detector reports `insufficient_data`, never "no regression". *Could not
tell* must never read as *all clear*.

### Why the second signal exists

The first end-to-end run **missed the regression**. Latency-only detection reported "no regression
detected" while every request was making 51 database round trips instead of 2.

The cause was Postgres cache warmth. The same N+1 fixture measured:

| | cold cache (slice 0) | warm cache (slice 1) |
|---|---|---|
| db spans per request | 2 → 51 | 2 → 51 |
| p95 latency | 156ms → 1753ms (**11.2x**) | 35ms → 51ms (**1.45x**) |

Identical work amplification; latency impact varying by almost an order of magnitude with nothing
but cache state. 1.45x fell below the 1.5x materiality threshold, so the detector stayed silent.

This is the project's own thesis arriving as a bug: latency says "the database got slower", span
count says "this code now makes 25x the queries". The detector now watches both, and work
amplification deliberately bypasses the latency sample floor because it is a ratio of counts.

### Verified result

```
db spans/req   2.0 -> 51.0  (25.5x)
p95 ratio      1.45x
Mann-Whitney p 0.000e+00

critical regression detected
  - database work per request rose 25.5x (2.0 -> 51.0 spans) in orders/repository.py
  - candidate is slower (Mann-Whitney p=0.00e+00 < 0.01)
```

It names the responsible file without being told where to look, from telemetry and SQL alone.
