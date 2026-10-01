"""The scheduled pipeline's shape.

Skipped unless Airflow is installed: it lives in an optional dependency group so
the core package and CI stay free of it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

airflow = pytest.importorskip("airflow", reason="airflow is an optional dependency group")

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ORDER = [
    "refresh_rollups",
    "data_quality",
    "detect",
    "incident_opened",
    "investigate",
]


@pytest.fixture(scope="module")
def dag():
    os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(ROOT / "dags"))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["aftermerge_pipeline"]


def test_the_dag_imports_without_errors(dag) -> None:
    assert len(dag.tasks) == len(EXPECTED_ORDER)


def test_the_quality_gate_sits_before_detection(dag) -> None:
    """Finding a regression in unvalidated data produces a confident answer about
    numbers nobody checked, which is worse than no answer."""
    order = [t.task_id for t in dag.topological_sort()]
    assert order == EXPECTED_ORDER
    assert order.index("data_quality") < order.index("detect")


def test_every_task_retries(dag) -> None:
    """Safe only because each step is idempotent; a non-idempotent task that
    retried would corrupt the rollup it was rebuilding."""
    assert all(task.retries >= 1 for task in dag.tasks)


def test_backfill_is_disabled(dag) -> None:
    """Replaying missed intervals would re-detect historical deploys as if they
    were happening now."""
    assert dag.catchup is False
    assert dag.max_active_runs == 1


def test_detection_branches_on_the_audit_trail_not_an_exit_code(dag) -> None:
    """`aftermerge detect` exits zero whether or not it finds anything, so the
    exit code cannot drive the branch."""
    from airflow.providers.standard.operators.python import ShortCircuitOperator

    branch = dag.get_task("incident_opened")
    assert isinstance(branch, ShortCircuitOperator)


def test_tasks_run_from_the_project_root(dag) -> None:
    """Commands read infra/ and .env by relative path."""
    for task_id in ("refresh_rollups", "data_quality", "detect", "investigate"):
        assert dag.get_task(task_id).cwd
