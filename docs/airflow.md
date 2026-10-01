# Scheduling the pipeline

`dags/aftermerge_pipeline.py` runs the read side of AfterMerge on a schedule:

```
refresh_rollups -> data_quality -> detect -> incident_opened -> investigate
```

Airflow is an **optional dependency group**. The default environment -- the one
CI builds with `uv sync --frozen` -- does not contain it, so nothing in the
pipeline's own test suite depends on a scheduler being installed:

```bash
uv sync --group airflow
export AIRFLOW_HOME="$PWD/.airflow"
export AIRFLOW__CORE__DAGS_FOLDER="$PWD/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
uv run --group airflow airflow dags test aftermerge_pipeline
```

`tests/unit/test_dag.py` asserts the structural properties below. It calls
`pytest.importorskip("airflow")`, so it verifies them locally and skips in CI
rather than failing there.

## Why the quality gate precedes detection

Detection is a statistical claim about a population of spans. Running it on data
that has not been checked produces a *confident* answer about numbers nobody
validated -- which is worse than no answer, because it looks like a result.

So `aftermerge dq` sits between the load and the analysis, and its exit code is
load-bearing: 1 when a *blocking* check fails, 2 when no store could be reached.
Either value fails the task, and Airflow then refuses to run anything
downstream. An advisory finding -- one metric reading zero for a legitimate
reason -- exits 0 and does not stop the run. See
[data-quality.md](data-quality.md) for that distinction.

## The three outcomes

All three were run end to end rather than reasoned about.

**Stale telemetry.** `freshness` blocks, and nothing downstream starts:

```
refresh_rollups  success
data_quality     up_for_retry -> failed
detect           (unrunnable)
investigate      (unrunnable)
DagRun Finished  state=failed
```

That is the gate working, not the DAG breaking.

**A regression present.** All five tasks run:

```
refresh_rollups  success
data_quality     success     (1 advisory: no error spans in a latency regression)
detect           success
incident_opened  success
investigate      success
DagRun Finished  state=success   run_duration=16.9s
```

**A quiet hour.** Detection finds nothing, the branch short-circuits, and the
run is still a *success* -- "no regression today" is not a failure:

```
refresh_rollups  success
data_quality     success
detect           success
incident_opened  success -> Skipping downstream tasks
investigate      (skipped)
DagRun Finished  state=success
```

A pipeline that went red on quiet hours would train everyone to ignore it.

## Why retries are safe

`default_args` sets `retries: 2`. A scheduler's first response to a failed task
is to run it again, so a step that is not idempotent cannot be retried -- and a
step that cannot be retried needs a human awake to handle it.

Each task survives a second run:

| Task | Why running it twice is safe |
|---|---|
| `refresh_rollups` | Drops and rebuilds whole day partitions; re-running rewrites the same rows (see [warehouse.md](warehouse.md)) |
| `data_quality` | Read-only |
| `detect` | Read-only apart from opening an incident, and the window below absorbs the duplicate |
| `incident_opened` | Read-only |
| `investigate` | Rewrites `incident-report.md` in place |

## Why `incident_opened` is a ShortCircuitOperator and not a sensor

`aftermerge detect` exits 0 whether or not it finds a regression: "no regression
today" is a successful run, not a failure. So the exit code cannot drive the
branch -- a quiet day and a detected regression look identical to Bash.

The branch reads the audit trail instead, and short-circuits when no incident
was opened in the last `INCIDENT_WINDOW_MINUTES` (180, overridable with
`AFTERMERGE_INCIDENT_WINDOW_MINUTES`). That window is deliberately wider than
the hourly schedule so a *retried* run still sees the incident its first attempt
opened rather than treating it as absent.

The callable imports `aftermerge` inside the function body, not at module
scope. Airflow re-parses every DAG file on a short interval; importing the whole
package on each parse would cost more than the work the DAG does.

## Other choices worth stating

- **`catchup=False`.** Backfilling would re-detect historical deploys as if they
  were happening now. There is no value in replaying missed intervals when the
  input is "what is live right now".
- **`max_active_runs=1`.** Two concurrent runs would have the rollup refresh
  dropping a partition while the other run's detection reads it.
- **`cwd=PROJECT_ROOT`.** The CLI resolves `infra/` and `.env` relatively, so
  tasks run from the project root rather than wherever the scheduler started.
  Override with `AFTERMERGE_HOME`.
- **Bare `aftermerge` in `bash_command`.** This assumes the scheduler's PATH
  contains the project venv, which is true when Airflow is itself launched
  through `uv run`. A deployment that starts Airflow some other way should use
  an absolute path to the entry point.
