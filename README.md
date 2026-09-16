# AfterMerge

Closed-loop production regression pipeline: **detect → investigate → reproduce → test → patch → verify → PR.**

Every conclusion is backed by a recorded artifact — a SQL result, a process exit code, a diff — rather than
a model's opinion. See [docs/PLAN.md](docs/PLAN.md) for the architecture and [docs/slice-0.md](docs/slice-0.md)
for the current build checklist.

## Status

| Slice | Scope | State |
|---|---|---|
| 0 / Phase A | Storage + telemetry pipe | **done** — verified 2026-09-16 |
| 0 / Phase B | Demo app, instrumentation, regression commit | **done** — verified 2026-09-16 |
| 0 / Phase C | Load, deploy dance, the query | **done** — verified 2026-09-16 |
| 1 / Store | Audit trail with enforced trust levels | **done** |
| 1 / Detector | Version windows, statistics, trigger rules | **done** |
| 1 / Evidence | Declarative fact set recorded per incident | **done** |
| 1 / Correlation | Changed files ∩ span code sites → hypotheses | **done** |
| 1 / Report | `aftermerge investigate` + e2e assertions | **done** |
| 2 / Sandbox | Ephemeral app at any commit, isolated telemetry | **done** |
| 2 / Capture | Sanitised production request envelopes | **done** |
| 2 / Replay | Differential replay → first level-3 verification | not started |
| 2 / Testgen | Generated test gated on fail@bad / pass@good | not started |

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
make up            # clickhouse + postgres + otel collector + shopdemo
make test          # unit tests (no Docker needed)
```

### Reproduce the slice 0 result

```bash
make truncate                              # clear previous telemetry
make dance                                 # deploy good sha, load, deploy bad sha, load
make facts                                 # the evidence
```

`make dance` defaults to the SHA pair pinned in `scenarios/n_plus_one.yaml`.
Override with `make dance GOOD=<ref> BAD=<ref> DUR=90s RPS=20`.

Expected output from `make facts`:

```
version   spans_per_request   p50_ms   p95_ms   p99_ms
cbb4790          2.0           12.3     156.4     770.0
8b4fd77         51.0           40.1    1753.1    7516.3
```

### Other targets

| command | does |
|---|---|
| `make probe` | emit a probe span over OTLP; prints its trace id |
| `make verify` | show recent spans in ClickHouse |
| `make ch` | open a ClickHouse shell |
| `uv run aftermerge deployments list` | show the recorded deploy history |
| `uv run aftermerge detect` | compare the last two deployed versions; open an incident if warranted |
| `uv run aftermerge incidents` | list detected incidents and their evidence |
| `uv run aftermerge capture` | reconstruct replayable requests from the regressed version |
| `make investigate` | detect, correlate with the diff, write `incident-report.md` |
| `make test` / `make test-all` | fast suite / including the slow docker sandbox tests |
| `make lint` | ruff check + format check |
| `make down` / `make reset` | stop the stack / also wipe volumes |

To look up one specific trace, call the script directly (Make would read the id
as a target name):

```bash
uv run python scripts/verify_span.py <trace_id>
```

## Layout

```
infra/           collector + clickhouse config
scripts/         phase A probe and verification
src/aftermerge/  the pipeline
  telemetry/     fact catalog: named .sql files + ClickHouse client
  store/         durable audit trail (Postgres), separate from the demo app's db
fixtures/        the demo app under test (phase B)
docs/            plan and per-slice checklists
```

## Ports

| Service | Port |
|---|---|
| ClickHouse HTTP | 8123 |
| ClickHouse native | 9000 |
| OTLP gRPC | 4317 |
| OTLP HTTP | 4318 |
| Collector health | 13133 |
| Postgres | 5432 |

Local development only — ClickHouse runs without a password on the default user.
