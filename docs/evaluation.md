# Model evaluation

`aftermerge evaluate` benchmarks models on the question that matters: **how often does their
output survive the gate?** Every run goes through the same validation the pipeline uses, so
"accepted" means what it means in production -- a test that fails on the defect and passes on its
predecessor, or a patch that restores both the query count and the response bytes.

```bash
uv run aftermerge evaluate --models claude-opus-5,claude-sonnet-5 --tasks testgen,patch,nl2sql
```

## Results, 2026-09-28

Scenario 001 (N+1), one attempt per cell, 20 sandbox builds.

| model | acceptance | tokens | cost | cost / accepted |
|---|---|---|---|---|
| claude-opus-5 | 100% | 3,657 | $0.1431 | $0.0715 |
| claude-sonnet-5 | 100% | 3,214 | $0.0220 | $0.0110 |
| claude-fable-5-1 | 100% | 3,590 | $0.0276 | $0.0138 |
| claude-haiku-4-5-20251001 | 50% | 2,490 | $0.0059 | $0.0059 |

**Cost per accepted result is the headline**, not cost per call. A model that is never accepted
costs infinitely more per useful result than an expensive one that is, and raw token price hides
that entirely.

## The one rejection

Haiku's generated test was rejected because it **failed on the good commit as well as the bad one**
(`cbb4790: expected to pass, exited 1`) -- a test that would have broken the build on healthy code.
That is precisely what the gate exists to stop, and no amount of reading the test would have caught
it as reliably as running it at both commits.

The exact source is not recoverable: rejected candidates are deleted by design, because an ungated
test left in `tests/regression/` is the false assurance this project is built to prevent. A
re-generation from the same prompt produced a different and acceptable threshold, so the model is
variable on this task rather than consistently wrong.

## Reading these numbers honestly

**One attempt per cell.** Haiku's "50%" is one acceptance out of two runs, not a stable rate, and
the re-generation above shows the variance is real. Treat the table as a measurement of this run,
not a benchmark ranking. Raising `--attempts` measures the retry loop instead, which is a different
question; repeating whole runs would be needed for a confident rate.

**One scenario.** Scenario 002 short-circuits before generation -- with work per request unchanged
there is no assertion that separates the commits, so `discriminates` returns false and no model is
ever called. That is correct behaviour, and it means 002 cannot contribute to a model benchmark.

**Prices are a local constant.** `harness.PRICES` is editable and may drift. Tokens are what this
harness measures; dollars are derived.

## Benchmarking does not disturb the repository

The certified test and candidate patch are snapshotted and restored around the matrix, and the
known-good certified test is restored before every patch run -- otherwise a model would be judged
against whichever test the previous run happened to leave behind.

## Tasks

| task | accepted means | needs an incident |
|---|---|---|
| `testgen` | the test failed on the defect and passed on its predecessor | yes |
| `patch` | the patch restored the query count and the response bytes | yes |
| `nl2sql` | the generated SQL agreed with a hand-written catalog query on every reference case | no |

`nl2sql` is answerable from telemetry alone, so a run benchmarking only that task does not require a
detected incident -- requiring one would refuse work that is perfectly possible. See
[nl2sql.md](nl2sql.md), including why its first results were a measurement of harness bugs rather
than of the models.
