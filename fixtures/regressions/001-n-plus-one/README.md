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
