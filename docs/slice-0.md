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

**Gotchas:**
- Must be the **contrib** collector image. The core image has no ClickHouse exporter and fails with an unhelpful config error.
- `otel_traces` does not exist yet. The exporter creates it on first write (`create_schema: true` by default). An empty database here is correct.

---

### A2. Prove the path with a throwaway emitter
A ~15-line script: create one span, export OTLP to `localhost:4317`, exit. Then query ClickHouse for it.

**Exit:** `SELECT * FROM otel_traces` returns your one span.

**Why this is its own step:** it isolates plumbing from application code completely. Skip it and the first time spans fail to arrive you will be debugging four services at once instead of one config file.

**Gotcha:** `BatchSpanProcessor` does not flush on exit. Call `provider.force_flush()` or you will see an empty table and blame the collector.

---

## Phase B — The fixture (the system under test)

### B1. Demo app, good version
- `fixtures/shopdemo/orders/` — FastAPI + asyncpg. `GET /orders?limit=50` returns orders with their line items, fetched with **one batched query**.
- `fixtures/shopdemo/gateway/` — FastAPI + httpx, calls orders over HTTP.
- Postgres schema: `customers`, `orders`, `order_items`. Seed script.

**Exit:** `curl localhost:8000/orders` returns JSON.

**Gotcha:** seed enough data that the N+1 actually hurts. ~50 orders per page with items each. Five rows will not produce a visible regression and the whole demo falls flat.

---

### B2. Instrument both services
Auto-instrumentation: `fastapi`, `httpx`, `asyncpg`. Resource attributes:
`service.name`, `service.version` (from `GIT_SHA` env), `deployment.environment`.

**Exit:** one request produces **one trace_id** with spans from *both* services, with correct parent/child links. Verify in SQL, not a UI.

**Gotchas:**
- Context propagation across the gateway→orders hop needs the httpx instrumentor active and the `traceparent` header surviving. This is the most common silent failure — you get two disconnected traces instead of one.
- Confirm asyncpg emits **one span per query**, and note the span naming, since your query filters on it. This is the core assumption of the whole project — verify it explicitly rather than assuming.

---

### B3. Custom span processor for source location  ⚠ RISK
An `on_start` hook that walks the call stack, finds the first frame inside your application source tree (skipping `site-packages`), and attaches `code.file.path`, `code.function.name`, `code.line.number`.

**Exit:** database spans carry `SpanAttributes['code.file.path'] = '.../orders/repository.py'`.

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

**Gotchas:**
- Must be genuine git history, not a feature flag or env toggle. The reproducer uses `git worktree` on these SHAs in slice 2, and PR generation needs a real diff.
- Keep the diff tight. A noisy diff gives change correlation nothing clean to point at.

---

## Phase C — The demonstration

### C1. Load generator
k6 script against `GET /orders`, ~20 rps, short ramp then steady state.

**Exit:** sustained traffic, spans accumulating in ClickHouse.

---

### C2. The deploy dance
Run traffic at `good_sha`. Stop. Rebuild with `GIT_SHA=bad_sha`. Run traffic again.

**Exit:** `SELECT DISTINCT ResourceAttributes['service.version'] FROM otel_traces` returns **two** values.

**Gotcha:** the `GIT_SHA` env var must actually change between runs. Forget it and both windows carry the same version, your `GROUP BY` collapses to one row, and nothing about the demo works. Check this before running traffic, not after.

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

```
a1b2c3d     1.0    ~300
e4f5g6h    50.0   ~4000
```

---

## Dependency order

A1 → A2 gate everything. B1 → B2 → B3 are sequential. B4 can be written any time after B1.
C1 needs B1; C2 needs B4; C3 needs everything.

B3 is the only step that can fail in a way that changes the architecture. If you want the risk retired
earliest, do a throwaway spike of the stack-walking processor during A2, before building the app.
