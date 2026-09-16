# 001 — N+1 query in order listing

| | |
|---|---|
| Service | `orders` |
| Route | `GET /orders` |
| File | `fixtures/shopdemo/orders/repository.py` |
| Branch | `regression/001-n-plus-one` |
| good_sha | `cbb4790` |
| bad_sha | `8b4fd77` |

## What changed

A batched item fetch (`WHERE order_id = ANY($1::bigint[])`) is replaced by a
per-order query inside a loop. The response shape is identical, so no functional
test catches it.

## Measured effect

At `limit=50`, application database queries per request:

| version | queries/request |
|---|---|
| good | 2 |
| bad | 51 |

Every one of those spans carries `code.file.path = orders/repository.py`, so
change correlation can intersect them with `git diff --name-only` instead of
inferring from timestamps.

## Why the commit message is bland

"Simplify order item lookup" is deliberate. A regression labelled as a regression
would let the investigator cheat; the demo is only meaningful if the conclusion
comes from telemetry rather than from reading the commit subject.

## Candidate fixes

| file | strategy | expected outcome |
|---|---|---|
| `fix.patch` | revert | validates: 2.0 vs 2.0 operations, byte-identical responses |
| `cheat.patch` | "repair" | **rejected** by response equivalence |

`cheat.patch` exists to prove the validator is worth having. It stops the per-order query by
returning every order with an empty `items` list, so database work becomes *constant* -- better than
the baseline -- and the certified regression test passes. Two of the three substantive checks go
green on a patch that silently drops every line item from the response:

```
passed   patch_regression_test: passes at the patched commit
passed   patch_work_restored: 2.0 vs 1.0 database operations per request
failed   patch_response_equivalence: body length 17503 became 6320 (-11183 bytes)
```

This is Goodhart's law inside the pipeline: the regression test became the target, so it stopped
measuring what it measured. The test is necessary and not sufficient, which is why validation also
requires the patched build to answer byte-identically to the known-good one.
