# Slice 0 — checklist

**Goal:** one SQL query that shows database spans per request going 1 → 50 across a deploy boundary.

**Not in scope:** detector, statistics, LLM, report, dashboard, reproduction, repair.

---

## Phase A — Plumbing (derisk before writing any app code)

### A1. Infrastructure skeleton
`docker-compose.yml` with three services and nothing emitting yet:
- `clickhouse/clickhouse-server` (ports 8123 HTTP, 9000 native)
- `postgres:17`
- `otel/opentelemetry-collector-contrib` (ports 4317 gRPC, 4318 HTTP)

`infra/otel-collector.yaml`: OTLP receiver → batch processor → clickhouse exporter.

**Exit:** `docker compose up` is clean; ClickHouse answers `SELECT 1`; collector logs show no export errors.

**DONE 2026-09-16.** ClickHouse 25.3, Postgres 17, collector 0.137.0 all healthy.

**Gotchas:**
- Must be the **contrib** collector image. The core image has no ClickHouse exporter and fails with an unhelpful config error.
- **Do not use `clickhouse-server:25.8`.** Its arm64 build ships a 0-byte `/entrypoint.sh` and the
  container dies with `exec format error` while the 344 MB `clickhouse` binary sits there intact.
  25.3 LTS, 24.8 and `latest` are all fine. Verify any version bump actually starts.
- `otel_traces` does not exist yet. The exporter creates it on first write (`create_schema: true` by default). An empty database here is correct.

---

### A2. Prove the path with a throwaway emitter
A ~15-line script: create one span, export OTLP to `localhost:4317`, exit. Then query ClickHouse for it.

**Exit:** `SELECT * FROM otel_traces` returns your one span.

**Why this is its own step:** it isolates plumbing from application code completely. Skip it and the first time spans fail to arrive you will be debugging four services at once instead of one config file.

**Gotcha:** `BatchSpanProcessor` does not flush on exit. Call `provider.force_flush()` or you will see an empty table and blame the collector.

**DONE 2026-09-16.** Parent + child span round-tripped; parent/child linkage intact;
`service.version`, `deployment.environment` and `code.file.path` all queryable from ClickHouse.
The exporter created `otel_traces` plus the `otel_traces_trace_id_ts` index materialized view.

---

## Phase B — The fixture (the system under test)

### B1. Demo app, good version
- `fixtures/shopdemo/orders/` — FastAPI + asyncpg. `GET /orders?limit=50` returns orders with their line items, fetched with **one batched query**.
- `fixtures/shopdemo/gateway/` — FastAPI + httpx, calls orders over HTTP.
- Postgres schema: `customers`, `orders`, `order_items`. Seed script.

**Exit:** `curl localhost:8000/orders` returns JSON.

**DONE.** Seeded 200 customers / 2,000 orders / 8,000 items. Baseline: **2** application queries per request.

**Gotcha:** seed enough data that the N+1 actually hurts. ~50 orders per page with items each. Five rows will not produce a visible regression and the whole demo falls flat.

---

### B2. Instrument both services
Auto-instrumentation: `fastapi`, `httpx`, `asyncpg`. Resource attributes:
`service.name`, `service.version` (from `GIT_SHA` env), `deployment.environment`.

**Exit:** one request produces **one trace_id** with spans from *both* services, with correct parent/child links. Verify in SQL, not a UI.

**DONE.** One trace, 10 spans, 2 services; gateway root -> httpx client -> orders server -> asyncpg spans. Context propagation confirmed.

**Gotchas:**
- Context propagation across the gateway→orders hop needs the httpx instrumentor active and the `traceparent` header surviving. This is the most common silent failure — you get two disconnected traces instead of one.
- Confirm asyncpg emits **one span per query**, and note the span naming, since your query filters on it. This is the core assumption of the whole project — verify it explicitly rather than assuming.

---

### B3. Custom span processor for source location  ⚠ RISK
An `on_start` hook that walks the call stack, finds the first frame inside your application source tree (skipping `site-packages`), and attaches `code.file.path`, `code.function.name`, `code.line.number`.

**Exit:** database spans carry `SpanAttributes['code.file.path'] = '.../orders/repository.py'`.

**DONE — risk retired.** Emits repo-relative `orders/repository.py`, directly comparable to `git diff --name-only`.
asyncpg's pool-reset query is correctly left unattributed (no application frame in its stack).

**Why it is separate:** this is the riskiest assumption in the architecture. Deterministic change correlation (intersecting a PR's changed files with span code sites) depends entirely on it. If it proves infeasible, you fall back to time-window correlation — weaker, and far better to learn now than in slice 3.

**Gotcha:** do **not** use `inspect.stack()`. It is extremely slow and you will be creating ~1000 spans/sec. Walk frames manually with `sys._getframe()`.

---

### B4. The regression, as a real commit
Branch off, replace the batched query with a per-order loop, commit.

```python
# good_sha
orders = await db.get_orders(order_ids)

# bad_sha
orders = [await db.get_order(oid) for oid in order_ids]
```

**Exit:** two real commit SHAs, and `git diff good..bad` is small and surgical.

**DONE.** good_sha `cbb4790` (main) / bad_sha `8b4fd77` (`regression/001-n-plus-one`).
One file, 15 insertions / 15 deletions. Measured **2 -> 51** queries per request.

**Gotchas:**
- Must be genuine git history, not a feature flag or env toggle. The reproducer uses `git worktree` on these SHAs in slice 2, and PR generation needs a real diff.
- Keep the diff tight. A noisy diff gives change correlation nothing clean to point at.

---

## Phase C — The demonstration

### C1. Load generator
k6 script against `GET /orders`, ~20 rps, short ramp then steady state.

**Exit:** sustained traffic, spans accumulating in ClickHouse.

**DONE.** k6 via `grafana/k6` (no local install). `constant-arrival-rate`, not `constant-VUs`:
a VU-based model sends *fewer* requests as responses slow, hiding the regression in the very
metric being measured. Dropped iterations rose 8 -> 127, itself a signal.

---

### C2. The deploy dance
Run traffic at `good_sha`. Stop. Rebuild with `GIT_SHA=bad_sha`. Run traffic again.

**Exit:** `SELECT DISTINCT ResourceAttributes['service.version'] FROM otel_traces` returns **two** values.

**Gotcha:** the `GIT_SHA` env var must actually change between runs. Forget it and both windows carry the same version, your `GROUP BY` collapses to one row, and nothing about the demo works. Check this before running traffic, not after.

**Gotcha, hit for real:** `docker compose run --rm loadgen` runs with `GIT_SHA` unset, so
`image: shopdemo:${GIT_SHA:-dev}` resolves to `shopdemo:dev`. Compose decides the running
containers no longer match the config and **recreates them from the dev image** -- silently
reverting the deploy and tagging every span `dev`. The first full run produced one version and
2.0 spans/request for both sides before this was spotted. Fix: `run --rm --no-deps`, export
`GIT_SHA`, and re-assert the served version *after* the load, not only before it.

**DONE.** Both services confirmed serving the expected SHA before and after each load run.

No `deployments` table yet — in slice 0 the `service.version` attribute *is* the deploy record.

---

### C3. The query
`src/aftermerge/telemetry/queries/span_count_per_trace.sql` plus a thin `clickhouse-connect` runner.

```sql
SELECT ResourceAttributes['service.version'] AS version,
       count() / uniqExact(TraceId)         AS db_spans_per_request,
       quantile(0.95)(Duration) / 1e6       AS p95_ms
FROM otel_traces
WHERE ServiceName = 'orders' AND SpanKind = 'Client'
GROUP BY version
ORDER BY version
```

**Exit — and the exit condition for all of slice 0:**

**DONE 2026-09-16.** 20 rps, 90s per side:

```
version   spans_per_request   p50_ms   p95_ms   p99_ms
cbb4790          2.0           12.3     156.4     770.0
8b4fd77         51.0           40.1    1753.1    7516.3
```

25.5x more database spans per request; p95 up 11.2x. Every span attributed to
`orders/repository.py`. No LLM involved -- two SQL files and a GROUP BY.

---

## Dependency order

A1 → A2 gate everything. B1 → B2 → B3 are sequential. B4 can be written any time after B1.
C1 needs B1; C2 needs B4; C3 needs everything.

B3 is the only step that can fail in a way that changes the architecture. If you want the risk retired
earliest, do a throwaway spike of the stack-walking processor during A2, before building the app.
