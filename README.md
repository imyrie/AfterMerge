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

## Quickstart (Phase A)

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
make up          # clickhouse + postgres + otel collector
make probe       # emit a probe span over OTLP
make verify      # confirm it reached ClickHouse
```

`make probe` prints a trace id; `make verify <trace_id>` looks up that specific trace.
`make ch` opens a ClickHouse shell, `make down` stops the stack, `make reset` also wipes volumes.

## Layout

```
infra/           collector + clickhouse config
scripts/         phase A probe and verification
src/aftermerge/  the pipeline (grows from slice 1)
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
