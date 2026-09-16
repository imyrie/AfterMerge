# AfterMerge — engineering plan

Closed-loop production regression pipeline: detect → investigate → reproduce → test → patch → verify → PR.
Every conclusion is backed by a recorded artifact (a SQL result, a process exit code, a diff), not by a model's opinion.

---

## 0. Positioning

OnePatch (the reference point) is **read-only**: it watches telemetry and PRs, finds observability gaps, and
writes investigations. It stops at the report.

AfterMerge's differentiator is the **half of the loop OnePatch doesn't close**: reproduce the failure, generate a
test that provably fails before the fix and passes after, patch, and gate the patch on executable evidence.

That means the engineering centre of gravity is **the verification harness, not the LLM**. Build that first-class
and the project reads as infrastructure work. Skip it and it reads as a prompt wrapper.

**The demo is the product.** The target experience for anyone evaluating this:

```bash
docker compose up -d
aftermerge demo run n_plus_one     # ~6 min: seeds traffic, ships the bad commit, investigates, patches
open http://localhost:3000/incidents/latest
```

---

## 1. Trust levels as a schema constraint

The three trust levels are the best idea in the original sketch. Make them **tables with enforced invariants**,
not documentation.

| Level | Table | Written by | Rule |
|---|---|---|---|
| 1. Observed fact | `facts` | Query runner only | Must store the exact query + params + raw result. Reproducible by re-running. |
| 2. Inference | `hypotheses` | Correlator or LLM | `supporting_fact_ids` must be non-empty. Ranked, never asserted. |
| 3. Verified conclusion | `verifications` | Sandbox runner only | Must carry a real process `exit_code` and captured stdout. **No LLM write path exists.** |

Enforce level 3 in code: the `verifications` repository takes a `CompletedProcess`, not a string. An LLM
physically cannot produce that object. This single design decision is what makes the project defensible
in an interview.

Every number that appears in a report must carry a `fact_id`. Prose is generated; numbers are looked up.

---

## 2. File structure

One installable Python package with subpackages — not a uv workspace of 10 distributions. The workspace
split is ceremony at this size and slows you down.

```
aftermerge/
├── README.md
├── pyproject.toml                 # uv, ruff, mypy, pytest config
├── docker-compose.yml             # clickhouse, postgres, otel-collector, prometheus, grafana, demo app
├── justfile                       # just demo, just up, just test
├── .env.example
│
├── src/aftermerge/
│   ├── core/                      # pure domain. zero I/O. importable by everything.
│   │   ├── models.py              # pydantic: Fact, Hypothesis, Verification, Incident, Patch, Window
│   │   ├── trust.py               # promotion rules; the invariants above
│   │   ├── slo.py                 # SLO definition + burn calculation
│   │   └── errors.py
│   │
│   ├── store/                     # durable audit trail  → Postgres
│   │   ├── db.py                  # SQLAlchemy 2.0 async engine/session
│   │   ├── tables.py              # typed ORM models
│   │   ├── repositories.py        # FactRepo, HypothesisRepo, VerificationRepo (narrow write APIs)
│   │   └── migrations/            # alembic
│   │
│   ├── telemetry/                 # analytical read side → ClickHouse
│   │   ├── client.py              # clickhouse-connect wrapper, typed results
│   │   ├── queries/               # ONE .sql FILE PER NAMED FACT. this dir is the heart of the project.
│   │   │   ├── latency_quantiles.sql
│   │   │   ├── error_rate.sql
│   │   │   ├── span_count_per_trace.sql        ← the "1 → 50 db spans" query
│   │   │   ├── dependency_attribution.sql
│   │   │   ├── exemplar_traces.sql
│   │   │   └── code_sites_in_traces.sql        ← span code.filepath/code.function inventory
│   │   ├── catalog.py             # registry: name → sql + param schema + result schema
│   │   └── capture.py             # request-envelope capture + PII sanitization
│   │
│   ├── detector/                  # "is something wrong?"  100% deterministic
│   │   ├── windows.py             # baseline vs candidate window construction
│   │   ├── stats.py               # quantiles, Mann–Whitney U, effect size, min-sample guards
│   │   └── rules.py               # SLO breach + regression triggers → Incident
│   │
│   ├── investigator/              # "what changed, and where?"
│   │   ├── evidence.py            # runs the fact catalog against the incident window → Fact rows
│   │   ├── topology.py            # span tree reconstruction, per-dependency time attribution
│   │   ├── change_correlation.py  # deploy window overlap + code-site set intersection
│   │   ├── code_map.py            # git diff → changed files/symbols (python ast / tree-sitter)
│   │   └── rank.py                # → ranked Hypothesis rows
│   │
│   ├── reproducer/
│   │   ├── worktree.py            # git worktree add at an arbitrary sha
│   │   ├── sandbox.py             # ephemeral docker compose project, isolated CH database
│   │   ├── replay.py              # send captured envelopes, collect the resulting traces
│   │   └── differential.py        # run the SAME fact queries at good_sha vs bad_sha
│   │
│   ├── testgen/
│   │   ├── prompts/
│   │   ├── generate.py
│   │   └── validate.py            # gate: MUST fail at bad_sha, MUST pass at good_sha, or reject
│   │
│   ├── repair/
│   │   ├── prompts/
│   │   ├── propose.py             # candidate patches (n=3), constrained to implicated files
│   │   ├── gates.py               # the verification gate stack (§6)
│   │   └── pr.py                  # branch, commit, PR body assembled from Fact rows
│   │
│   ├── instrumentor/              # phase 4 — static analysis, separable, ships its own PR type
│   │   ├── rules/                 # uninstrumented_route.py, untraced_egress.py, swallowed_exception.py,
│   │   │                          # missing_timeout.py, unbounded_query.py
│   │   ├── scan.py
│   │   └── codemod.py             # libcst
│   │
│   ├── pipeline/                  # orchestration
│   │   ├── step.py                # Step protocol: pydantic in → pydantic out, content-addressed
│   │   ├── runner.py              # resumable state machine, persists every step
│   │   └── graph.py               # the pipeline definition
│   │
│   ├── llm/
│   │   ├── client.py              # anthropic SDK, retries, token accounting
│   │   ├── schemas.py             # every call is structured output, never free text into logic
│   │   └── budget.py
│   │
│   ├── report/
│   │   ├── render.py              # Fact rows → markdown / JSON
│   │   └── templates/
│   │
│   ├── api/                       # FastAPI: read-only view of investigations
│   └── cli/                       # typer: aftermerge detect|investigate|reproduce|repair|demo
│
├── fixtures/                      # NOT the product. the system under test.
│   ├── shopdemo/
│   │   ├── gateway/               # FastAPI
│   │   ├── orders/                # FastAPI + postgres  ← where regressions land
│   │   ├── payments/              # FastAPI, deliberately flaky
│   │   ├── worker/
│   │   ├── compose.yml
│   │   └── loadgen/               # k6 scripts
│   └── regressions/               # each is a committable patch, not a feature flag
│       ├── 001-n-plus-one/
│       ├── 002-missing-timeout/
│       ├── 003-cache-key-collision/
│       └── 004-unbounded-serialization/
│
├── scenarios/                     # declarative demo definitions (§7)
│   ├── n_plus_one.yaml
│   └── payment_timeout.yaml
│
├── infra/
│   ├── otel-collector.yaml
│   ├── clickhouse/init.sql
│   ├── prometheus.yml
│   └── grafana/dashboards/
│
├── web/                           # Next.js dashboard
│   └── app/incidents/[id]/        # report + trace waterfall + before/after diff
│
└── tests/
    ├── unit/
    ├── integration/               # testcontainers: real ClickHouse + Postgres
    └── e2e/test_scenarios.py      # runs each scenario, asserts its `expect:` block
```

### Why `telemetry/queries/*.sql` matters

Keeping fact queries as named, parameterized SQL files (not inline strings, not ORM chains) gives you:
- a fact catalog you can enumerate in the UI,
- identical query code in production and in replay sandboxes,
- an audit trail where `facts.query_name` + `facts.query_params` fully reproduces any number in a report.

---

## 3. Library and tool choices

### Keep
| Concern | Pick | Why this one |
|---|---|---|
| Packaging | **uv** | Already installed. Fast, lockfile, `uv run` is the whole dev story. |
| Lint/format | **ruff** | Replaces black + isort + flake8. |
| Types | **mypy** strict on `core/` and `store/` | Keep strictness where the invariants live. |
| Contracts | **pydantic v2** | Every step boundary and every LLM response. |
| API | **FastAPI** | Also what the demo app uses — one instrumentation story. |
| Audit DB | **PostgreSQL + SQLAlchemy 2.0 + Alembic** | Relational integrity is the point here. |
| Trace store | **ClickHouse + clickhouse-connect** | §3.1 |
| Telemetry | **OpenTelemetry SDK + auto-instrumentation** (fastapi, httpx, asyncpg/psycopg, sqlalchemy) | Span-per-query for free — that's your N+1 signal. |
| Pipe | **OTel Collector (contrib)** with `clickhouse` exporter + `spanmetrics` connector | Zero custom ingest code. |
| Stats | **scipy** + **numpy** | Mann–Whitney U, bootstrap CIs. |
| Git | **GitPython** (worktrees, diffs) + **unidiff** (hunk parsing) | |
| GitHub | **PyGithub** | |
| CLI | **typer** + **rich** | The terminal report is a demo surface. |
| Replay | **httpx** | |
| Codemod | **libcst** | Preserves formatting — required for a reviewable instrumentation PR. |
| Load | **k6** | Better output than locust, scriptable scenarios. |
| Tests | **pytest**, **pytest-asyncio**, **testcontainers** | Integration tests against real CH/PG is a strong signal. |
| LLM | **anthropic** SDK — `claude-opus-5` for patches, `claude-sonnet-5` for summaries | |
| Dashboard | **Next.js + Tailwind + shadcn/ui**; hand-rolled SVG waterfall | The waterfall is the screenshot people remember. |

### 3.1 Why ClickHouse and not Postgres / Jaeger / Tempo

The core evidence query is *"how many spans of name X occur per trace, before vs after"* — a grouped
aggregation over millions of rows. ClickHouse answers it in plain SQL in milliseconds.

- **Jaeger** — a trace *viewer*. Its query API can't aggregate across traces. Dead end for the investigator.
- **Tempo** — TraceQL aggregation is limited and you'd fight it.
- **Postgres** — workable at demo volume, but you reimplement columnar aggregation and you lose the
  "this is how real observability backends work" story. SigNoz and Uptrace both use ClickHouse.

The OTel Collector's `clickhouseexporter` creates the `otel_traces` table for you (`Timestamp`, `TraceId`,
`SpanId`, `ParentSpanId`, `SpanName`, `SpanKind`, `ServiceName`, `Duration`, `SpanAttributes`,
`ResourceAttributes`, `StatusCode`). You write zero ingest code and query it directly.

### Cut or defer
- **Redis** — nothing in the pipeline needs a cache or a broker. Cut it. (If the demo app wants a cache for
  the `003-cache-key-collision` regression, that's a fixture dependency, not an AfterMerge one.)
- **Celery / Airflow / Temporal** — the pipeline is a resumable state machine in Postgres. Adding a
  workflow engine buys nothing and costs you a service.
- **Prometheus + Grafana** — keep them (the `spanmetrics` connector feeds them for free, and a Grafana
  screenshot sells the README), but do **not** query them from the investigator. All analysis goes through
  ClickHouse so replay and production share one code path.
- **A real-time monitor daemon** — the detector should be a batch function over an explicit time range.
  Real-time is a `while True` wrapper you add in a day, later.

---

## 4. Two ideas that make correlation deterministic

These are the highest-leverage technical decisions. Both convert "the LLM guessed" into "SQL joined."

### 4.1 Stamp every span with the deployed commit

Set the OTel resource attributes at service startup:

```python
Resource.create({
    "service.name": "orders",
    "service.version": os.environ["GIT_SHA"],
    "deployment.environment": "demo",
    "aftermerge.deployment.id": os.environ["DEPLOY_ID"],
})
```

Now "did behaviour change after the deploy?" is a `GROUP BY ResourceAttributes['service.version']`.
You never have to infer the deploy boundary from timestamps alone — timestamps become corroborating
evidence, not the primary signal. This also survives overlapping / rolled-back deploys, which pure
time-window correlation cannot.

### 4.2 Stamp spans with their source location

OpenTelemetry semconv defines `code.file.path`, `code.function.name`, `code.line.number`. Emit them on
your manually created spans (and derive them for auto-instrumented DB spans via a small span processor
that walks the stack).

Then change correlation is a **set intersection**:

```
changed_files(PR #42) ∩ distinct(SpanAttributes['code.file.path']) over regressed traces
```

The result is a Level-1 fact — `"PR #42 modified orders/repository.py, which appears as the code site
of 2,847 of the 2,851 new database spans"` — not a Level-2 inference. The LLM's job shrinks to writing
the sentence around a number it did not compute.

---

## 5. Data flow

### 5.1 Steady state

```
fixtures/shopdemo services
   │  OTLP/gRPC  (spans carry service.version = git sha, code.file.path)
   ▼
OTel Collector ──┬── clickhouseexporter ──► ClickHouse.otel_traces
                 └── spanmetrics connector ──► Prometheus  (dashboards only)

deploy script ──► Postgres.deployments (sha, prev_sha, pr_number, deployed_at)
demo app middleware ──► Postgres.captured_requests  (sampled: slow or 5xx, sanitized)
```

### 5.2 The pipeline

```
[1] DETECT          ClickHouse: quantiles + error rate, baseline window vs candidate window
                    scipy: Mann–Whitney U + effect ratio, n≥100 guard
                    ─► Incident(route, onset_at, severity)          ── deterministic

[2] GATHER          run every query in the fact catalog against the incident window
                    ─► Fact[]  (LEVEL 1: latency delta, error rate, span_count_per_trace
                                 delta, dependency attribution, exemplar trace ids)

[3] CORRELATE       Postgres.deployments ⋈ spans by service.version
                    git diff(prev_sha..sha) → changed files
                    changed files ∩ span code.file.path over regressed traces
                    ─► more Fact[] , then Hypothesis[]              ── LEVEL 2, ranked

[4] REPRODUCE       git worktree @ bad_sha and @ good_sha
                    docker compose up in isolated project + isolated CH database
                    replay captured_requests (N=50) against each
                    re-run THE SAME fact queries on replay traces
                    ─► Verification (differential)                  ── LEVEL 3

[5] TESTGEN         LLM writes tests/regression/test_<incident>.py from Fact[] + diff
                    gate: pytest @ bad_sha must EXIT 1
                          pytest @ good_sha must EXIT 0
                    reject + retry (max 2) otherwise
                    ─► Artifact(test) + Verification

[6] REPAIR          LLM proposes ≤3 patches, restricted to files implicated by Fact[]
                    each candidate runs the full gate stack (§6)
                    ─► Artifact(patch) + Verification per gate

[7] REPORT + PR     render Fact[] / Hypothesis[] / Verification[] into markdown
                    branch + commit test + patch, open PR with the evidence table
```

Each step is a `Step` with a pydantic input and output, persisted in `runs` with an `input_hash`.
That means: resumable after a crash, cacheable in development, and replayable for a deterministic demo —
you can re-render an incident report a year later from the stored rows without re-running anything.

### 5.3 Schema sketch

```sql
deployments(id, service, repo, commit_sha, prev_commit_sha, pr_number, deployed_at, actor)

incidents(id, service, route, slo_id, onset_at, detected_at, severity, status)

facts(id, incident_id, kind, query_name, query_params jsonb, raw_result jsonb,
      value numeric, unit, source, observed_at)

hypotheses(id, incident_id, statement, kind, score,
           supporting_fact_ids uuid[] NOT NULL CHECK (cardinality(...) > 0),
           generated_by, created_at)

verifications(id, hypothesis_id, method, command text, exit_code int NOT NULL,
              stdout_ref, metrics jsonb, verdict, ran_at)

captured_requests(id, incident_id, trace_id, method, route, headers jsonb,
                  body jsonb, sanitized_at, replay_safe bool)

artifacts(id, incident_id, kind, path, blob_sha, created_at)   -- test | patch | report | pr

runs(id, incident_id, step, status, input_hash, output_ref, started_at, ended_at, error)
```

`hypotheses.score` is computed, not asked for: weighted sum of time proximity, code-site overlap
fraction, and service match. The LLM writes `statement`; the scorer writes `score`.

---

## 6. The gate stack

A patch is accepted only if every gate produces a `verifications` row with `exit_code == 0`:

1. `git apply` succeeds cleanly
2. existing test suite passes at the patched sha
3. the generated regression test passes at the patched sha
4. replay: target span count per trace ≤ baseline × 1.2
5. replay: p95 for the affected route ≤ SLO
6. diff touches only files named in `facts` and is ≤ N lines

Fail any gate → next candidate. All candidates fail → ship the **report and the test only**, and say so.
A pipeline that knows how to give up honestly is more credible than one that always produces a patch.

---

## 7. Scenarios: make the demo declarative

```yaml
# scenarios/n_plus_one.yaml
id: n_plus_one
title: N+1 query introduced in order listing
service: orders
regression: fixtures/regressions/001-n-plus-one/patch.diff
slo:
  route: "GET /orders"
  p95_ms: 500
  error_rate: 0.01
traffic:
  script: fixtures/shopdemo/loadgen/browse.js
  rps: 20
  baseline: 5m
  candidate: 5m
expect:
  detected: true
  root_cause_file: fixtures/shopdemo/orders/repository.py
  facts_present: [latency_p95_delta, span_count_per_trace_delta, code_site_overlap]
  patch_accepted: true
```

Two payoffs:
- adding regression #2 through #6 costs a YAML file and a diff, not new code;
- `tests/e2e/test_scenarios.py` asserts every `expect:` block, so **AfterMerge has an end-to-end test
  suite that tests AfterMerge on real telemetry**. That sentence belongs in the README.

---

## 8. Build order (vertical slices, each independently demoable)

| Slice | Contains | Done when |
|---|---|---|
| **0** | shopdemo (gateway + orders + pg), OTel → collector → ClickHouse, `span_count_per_trace.sql` | You can run one SQL query showing 1 → 50 db spans across the bad deploy. |
| **1** | detector + evidence + change correlation + markdown report + CLI | `aftermerge investigate` prints an evidence-backed report. **This alone is a complete portfolio project.** |
| **2** | reproducer sandbox + differential + testgen with the fail/pass gate | A generated test that provably fails at bad_sha, passes at good_sha. **This is the differentiator — don't skip to slice 3.** |
| **3** | repair + gate stack + GitHub PR | A merged-looking PR with an evidence table. |
| **4** | web dashboard + trace waterfall | The screenshot for the README and your resume link. |
| **5** | instrumentor static analysis (second PR type) | |
| **6** | canary guard: hold both versions under identical replayed traffic, promote only if error rate ≤ threshold and p95 ≤ SLO | |

Slice 0 is a weekend. Slices 1–2 are the project. Everything after is upside.

If you only ever finish slice 2, you have something most portfolio projects don't: **a claim that is
mechanically checked rather than asserted**.

---

## 9. LLM boundary

Allowed:
- prose summaries of already-computed facts
- explaining a diff hunk in English
- proposing candidate test code
- proposing candidate patches

Forbidden (enforced by having no code path):
- deciding whether a regression occurred
- deciding whether a hypothesis is verified
- producing any number that appears in a report

Every LLM call uses structured output with a pydantic schema, is logged with its full prompt and response
to `artifacts`, and is treated as a *proposal* that must survive a deterministic gate.

---

## 10. Housekeeping

- The repo is currently nested at `~/AfterMerge/AfterMerge`. Flatten it (`mv`) before there are two
  places called AfterMerge in your shell history.
- Pin the demo app's regressions as **real commits on a real branch** in the same repo, so `git worktree`
  and PR generation operate on genuine git history. Feature-flagging the regression would hollow out
  slices 2 and 3.
