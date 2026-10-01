# AfterMerge

[![CI](https://github.com/imyrie/AfterMerge/actions/workflows/ci.yml/badge.svg)](https://github.com/imyrie/AfterMerge/actions/workflows/ci.yml)

Closed-loop production regression pipeline: **detect → investigate → reproduce → test → patch → verify → PR.**

The loop is complete:

```bash
make up
aftermerge detect && aftermerge investigate   # find it, correlate it to a commit
aftermerge capture && aftermerge verify       # reproduce it in isolation
aftermerge certify                            # a test that fails@bad, passes@good
aftermerge fix && aftermerge pr               # a validated patch and a PR body
```

Every conclusion is backed by a recorded artifact — a SQL result, a process exit code, a diff — rather than
a model's opinion. See [docs/PLAN.md](docs/PLAN.md) for the architecture, [docs/airflow.md](docs/airflow.md)
for how the loop runs on a schedule, [docs/nl2sql.md](docs/nl2sql.md) for the gate on generated SQL,
and [docs/slice-0.md](docs/slice-0.md) for the current build checklist.

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
| 2 / Replay | Differential replay → first level-3 verification | **done** |
| 2 / Testgen | Regression test written from incident evidence | **done** |
| 2 / Gate | Automated fail@bad / pass@good validation | **done** |
| 3 / Validation | Patch guards, equivalence oracle, four checks | **done** |
| 3 / Proposal | Generate a candidate fix | **done** |
| 3 / Pull request | Branch, body, `--push` behind a flag | **done** |
| Scenario 002 | Inverse signal profile — latency up, work flat | **done** — see docs/scenario-002.md |
| Orchestration | Hourly Airflow DAG with a data-quality gate | **done** — see docs/airflow.md |
| Text to SQL | Gated metric requests, diffed against the catalog | **done** — see docs/nl2sql.md |

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

### Model-backed generation (optional)

Test and patch generation each have two implementations behind one interface. The deterministic ones
are the default and need no credentials; the model-backed ones need a key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # optional; AFTERMERGE_MODEL overrides the model
aftermerge certify --generator anthropic  # model writes the regression test
aftermerge fix --proposer anthropic       # model writes the patch
```

Nothing about the trust model changes. A model-written test is still accepted only if it fails on the
broken commit and passes on the fixed one; a model-written patch is still rejected unless it restores
the measured behaviour *and* returns byte-identical responses. The model proposes; the gates decide.

A rejected candidate's reasons are fed back into the next attempt, so a retry is informed rather than
a re-roll. Deterministic generators ignore that feedback and are never retried, since their output
cannot change.

Without a key, `--generator anthropic` fails with a clear message rather than quietly falling back to
the template -- every candidate records what produced it, and a silent substitution would make that
provenance a lie.

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
| `uv run aftermerge replay` | differential replay across two commits (exit 0 = reproduced) |
| `--seed default\|large` | sandbox dataset; volume-dependent faults need `large` (see scenarios) |
| `uv run aftermerge verify` | run the replay and record its outcome as level-3 evidence |
| `uv run aftermerge testgen` | write a regression test encoding the measured behaviour |
| `uv run aftermerge certify` | generate a test and keep it only if it passes the gate |
| `uv run aftermerge gate` | check a test fails@bad and passes@good (exit 0 = discriminates) |
| `uv run aftermerge validate` | prove a candidate fix removes the fault and changes nothing else |
| `uv run aftermerge fix` | propose a fix and keep it only if validation accepts it |
| `uv run aftermerge warehouse apply` | create the rollup tables |
| `uv run aftermerge warehouse refresh` | incrementally load only the days that changed |
| `uv run aftermerge warehouse benchmark` | verify rollups agree with raw, then report scan cost |
| `uv run aftermerge stream` | consume spans from Kafka and reach a verdict during a rollout |
| `uv run aftermerge dq` | check data quality; exits non-zero to block analysis on bad data |
| `uv run aftermerge ask "<question>"` | translate a metric request into gated, executed SQL |
| `uv run aftermerge evaluate` | benchmark models on how often their output survives the gate |
| `uv run aftermerge pr` | build a branch and PR body locally (`--push` / `--open` to go outward) |
| `make dag` | run the scheduled pipeline once end to end (needs the `airflow` group) |
| `make investigate` | detect, correlate with the diff, write `incident-report.md` |
| `make test` / `make test-all` | fast suite / including the slow docker sandbox tests |
| `make lint` | ruff check + format check |
| `make down` / `make reset` | stop the stack / also wipe volumes |

To look up one specific trace, call the script directly (Make would read the id
as a target name):

```bash
uv run python scripts/verify_span.py <trace_id>
```

## Continuous integration

Every push and pull request runs lint, format check and mypy, then the test suite with a PostgreSQL
service so the audit-trail integration tests execute rather than skip.

Airflow is an optional dependency group, so `uv sync --frozen` does not install it and the DAG
tests skip rather than fail on the runner. The orchestrator is a consumer of this pipeline, not
something the pipeline needs in order to be correct.

The `slow` marker stays out of CI. Those tests drive Docker Compose projects and reach a ClickHouse
container by name -- a local developer topology rather than something a runner provides. Run them
locally with `make test-all`.

## Layout

```
infra/           collector + clickhouse config
scripts/         phase A probe and verification
src/aftermerge/  the pipeline
  telemetry/     fact catalog: named .sql files + ClickHouse client
  store/         durable audit trail (Postgres), separate from the demo app's db
fixtures/        the demo app under test (phase B)
docs/            plan and per-slice checklists
dags/            airflow dag that runs the read side hourly
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
