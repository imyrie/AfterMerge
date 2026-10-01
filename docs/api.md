# Read API

`aftermerge serve` exposes the rollups over HTTP.

```bash
uv run aftermerge serve                      # http://127.0.0.1:8000, docs at /docs
curl 'localhost:8000/metrics/latency/trend?lookback_days=30'
```

| endpoint | metric |
|---|---|
| `GET /health` | whether the warehouse answers, not just whether this process started |
| `GET /catalog` | every metric definition with its parameters and types |
| `GET /metrics/latency` | latency quantiles per deployed version |
| `GET /metrics/latency/trend` | day-over-day p95 per version: rolling mean, delta, day index |
| `GET /metrics/db-work` | database spans per request, per version and code site |
| `GET /cache` | hits, misses, coalesced loads, evictions |

## Catalog-backed, not inline SQL

Every endpoint runs a named `.sql` file from the fact catalog with validated parameters. That is the
point of having a catalog: if the API computed p95 its own way, an answer from the API and an answer
from `aftermerge facts` could disagree, and neither would be wrong enough for anyone to notice.

`GET /catalog` is generated from those same files, so the documented signature of a metric cannot
drift from the one it actually has:

```json
{"name": "route_latency_trend",
 "description": "Day-over-day p95 latency per deployed version, read from the daily rollup.",
 "parameters": {"lookback_days": "UInt32", "route": "String", "service": "String"}}
```

## Provenance travels with the payload

```json
{"query_name": "latency_quantiles",
 "params": {"service": "gateway", "route": "GET /orders", "lookback_minutes": 240},
 "columns": ["version", "requests", "p50_ms", "p95_ms", "p99_ms", "errors"],
 "rows": [["cbb4790", 1789, 18.8, 515.0, 1075.5, 0],
          ["8b4fd77", 930, 79.9, 2510.5, 3363.5, 0]],
 "read_rows": 8192, "cached": false}
```

Every number names the statement and parameters that produced it, so a caller can reproduce it
without asking anyone — the same contract the audit trail enforces internally.

## Caching

The TTL is not a tuning knob picked by feel. These metrics come from daily rollups that the Airflow
DAG refreshes hourly ([airflow.md](airflow.md)), so an entry living past that refresh serves numbers
the warehouse has already corrected, while one living far shorter spends a warehouse scan to
re-derive a value that provably cannot have changed.

**Single-flight matters more than the hit rate.** Without it, N concurrent requests for one cold key
issue N identical warehouse scans — the moment the cache is most needed is the moment it would do
nothing. The cache holds its lock only around bookkeeping, never across a load, and three outcomes
are counted separately:

- `hits` — a live entry was returned
- `coalesced` — the caller waited on another request's in-flight load instead of duplicating it
- `misses` — this caller did the load

`coalesced` is counted apart from `hits` because it did not read a stored value; it avoided a
duplicate query, which is a different saving and worth seeing on its own. A failed load does not
poison the key, and a cancelled waiter cannot cancel the shared load and strand the others.

Eviction is oldest-expiry-first once `max_entries` is reached — a bound that discards whatever was
closest to useless anyway, without the bookkeeping an LRU would need.

## Bounds are load-bearing

`lookback_minutes` with no ceiling is a full-retention scan reachable from a URL. Each parameter is
bounded (`ge`/`le`), so an out-of-range request is rejected with 422 before the warehouse is touched
at all — verified against the running server:

```
lookback_days=100000 -> HTTP 422
```

The connector additionally applies `readonly=1` with execution-time and result-row caps, the same
caps the generated-SQL executor uses ([nl2sql.md](nl2sql.md)). A warehouse error becomes a 502 with
the reason, not a traceback.

## What building it found

The async client's `close()` is a coroutine. Calling it without awaiting returned an un-awaited
coroutine and leaked the underlying aiohttp session and TCP connector on **every shutdown**,
announced only by a `RuntimeWarning` that nothing was watching for. `close()` now awaits when the
result is awaitable, and a test asserts it.

Worth noting how close that came to shipping: the endpoints all worked, the data was right, and the
tests passed. The leak was visible only as warning text above otherwise correct output.

Also: `hasattr(clickhouse_connect, "get_async_client")` returns `True` without the `async` extra
installed. The symbol exists and raises on call, so the dependency is now declared as
`clickhouse-connect[async]`.

## Not authenticated

Binds to `127.0.0.1` by default. It applies read-only caps and bounds every parameter, but it
answers questions about production telemetry and has no authentication, so exposing it beyond
localhost is a deliberate act rather than the default.
