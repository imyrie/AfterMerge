"""Scheduled AfterMerge pipeline.

    refresh rollups -> data quality -> detect -> (incident?) -> investigate

Every task is a CLI command that already exists, exits non-zero on failure, and
is safe to run twice. That is not incidental: a scheduler's first response to a
failed task is to run it again, so a step that is not idempotent cannot be
retried, and a step that cannot be retried needs a human at 3am.

The quality gate sits *before* detection on purpose. Finding a regression in data
that has not been checked produces a confident answer about numbers nobody has
validated, which is worse than no answer -- so a failing check stops the run
rather than annotating it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.python import ShortCircuitOperator
from airflow.sdk import DAG

#: Commands read `infra/` and `.env` by relative path, so tasks run from the
#: project root rather than wherever the scheduler happens to start.
PROJECT_ROOT = os.environ.get("AFTERMERGE_HOME", "/Users/imyrie25/AfterMerge")

#: How recent an incident must be to count as this run's. Wider than the
#: schedule so a retried run still sees the incident its first attempt opened,
#: rather than opening a duplicate. Tunable because a deployment on a different
#: schedule needs a different window.
INCIDENT_WINDOW_MINUTES = int(os.environ.get("AFTERMERGE_INCIDENT_WINDOW_MINUTES", "180"))

DEFAULT_ARGS = {
    "owner": "aftermerge",
    # Safe because every task is idempotent: the rollup refresh drops and
    # rebuilds whole day partitions, and the remaining tasks only read.
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
    "depends_on_past": False,
}


def incident_was_opened(**_: object) -> bool:
    """Did detection actually open an incident?

    `aftermerge detect` exits zero whether or not it finds anything -- "no
    regression" is a successful run, not a failure -- so the exit code cannot
    drive the branch. The audit trail can.

    Imported inside the callable rather than at module scope: Airflow re-parses
    every DAG file on a short interval, and pulling in the whole package each
    time would make parsing slower than the work.
    """
    from datetime import UTC, datetime, timedelta

    from aftermerge.store import db as store_db
    from aftermerge.store.repositories import IncidentRepository

    cutoff = datetime.now(UTC) - timedelta(minutes=INCIDENT_WINDOW_MINUTES)
    with store_db.session_scope(store_db.get_engine()) as session:
        recent = [
            incident
            for incident in IncidentRepository(session).list_recent(limit=5)
            if incident.detected_at >= cutoff
        ]
    return bool(recent)


with DAG(
    dag_id="aftermerge_pipeline",
    description="Refresh rollups, gate on data quality, then detect and investigate",
    start_date=datetime(2026, 1, 1),
    schedule="@hourly",
    # Backfilling this pipeline would re-detect historical deploys as if they
    # were happening now, so runs are not replayed for missed intervals.
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["aftermerge", "telemetry"],
) as dag:
    refresh_rollups = BashOperator(
        task_id="refresh_rollups",
        bash_command="aftermerge warehouse refresh",
        cwd=PROJECT_ROOT,
    )

    data_quality = BashOperator(
        task_id="data_quality",
        # Exits 1 when a check fails and 2 when nothing could be checked. Either
        # way the task fails and nothing downstream runs, which is the point.
        bash_command="aftermerge dq",
        cwd=PROJECT_ROOT,
    )

    detect = BashOperator(
        task_id="detect",
        bash_command="aftermerge detect",
        cwd=PROJECT_ROOT,
    )

    incident_opened = ShortCircuitOperator(
        task_id="incident_opened",
        python_callable=incident_was_opened,
    )

    investigate = BashOperator(
        task_id="investigate",
        bash_command="aftermerge investigate --output incident-report.md",
        cwd=PROJECT_ROOT,
    )

    refresh_rollups >> data_quality >> detect >> incident_opened >> investigate
