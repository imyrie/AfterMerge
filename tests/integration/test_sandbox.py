"""Sandbox lifecycle. Slow: each test builds and runs containers.

uv run pytest -m slow tests/integration/test_sandbox.py
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from aftermerge.reproducer.envelope import RequestEnvelope
from aftermerge.reproducer.sandbox import sandbox

ROOT = Path(__file__).resolve().parents[2]
BAD_SHA = "8b4fd77"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return False
    # The shared ClickHouse is where sandbox collectors write.
    return (
        subprocess.run(
            ["docker", "inspect", "aftermerge-clickhouse"], capture_output=True
        ).returncode
        == 0
    )


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _docker_ready(), reason="docker or the shared stack is unavailable"),
]


@pytest.fixture(scope="module")
def running():
    with sandbox(BAD_SHA, repo_root=ROOT) as sb:
        envelope = RequestEnvelope.get("/orders", limit=50)
        httpx.get(f"{sb.base_url}{envelope.target}", timeout=60.0)
        sb.wait_for_spans(minimum=50)
        yield sb


def test_it_serves_the_requested_commit(running) -> None:
    """Reachability is not enough: a stale container answers health checks too."""
    assert running.health()["version"] == BAD_SHA


def test_the_port_is_ephemeral_not_the_dev_stack(running) -> None:
    """A fixed port would collide with the dev stack or a second sandbox."""
    assert ":8000" not in running.base_url
    assert ":8001" not in running.base_url


def test_traces_land_in_an_isolated_database(running) -> None:
    assert running.trace_database.startswith("repro_")
    assert running.span_count() > 0


def test_production_traces_are_untouched(running) -> None:
    """Isolation is structural: replay rows are not in `otel` at all."""
    result = subprocess.run(
        [
            "docker",
            "exec",
            "aftermerge-clickhouse",
            "clickhouse-client",
            "--query",
            "SELECT count() FROM otel.otel_traces "
            "WHERE ResourceAttributes['deployment.environment']='repro'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "0"


def test_the_regression_reproduces_in_isolation(running) -> None:
    """51 database operations per request, with no production traffic involved."""
    result = subprocess.run(
        [
            "docker",
            "exec",
            "aftermerge-clickhouse",
            "clickhouse-client",
            "--query",
            f"""SELECT round(count() / uniqExact(TraceId), 1)
             FROM {running.trace_database}.otel_traces
             WHERE ServiceName='orders' AND SpanKind='Client'
               AND SpanAttributes['code.file.path'] != ''""",
        ],
        capture_output=True,
        text=True,
    )
    assert float(result.stdout.strip()) == pytest.approx(51.0, rel=0.05)


def test_teardown_leaves_nothing_behind() -> None:
    """A crashed or finished run must not accumulate containers, volumes, or databases."""
    with sandbox(BAD_SHA, repo_root=ROOT) as sb:
        project, database = sb.project, sb.trace_database

    for kind, args in (
        (
            "containers",
            ["docker", "ps", "-a", "--filter", f"name={project}", "--format", "{{.Names}}"],
        ),
        (
            "volumes",
            ["docker", "volume", "ls", "--filter", f"name={project}", "--format", "{{.Name}}"],
        ),
    ):
        assert subprocess.run(args, capture_output=True, text=True).stdout.strip() == "", kind

    exists = subprocess.run(
        [
            "docker",
            "exec",
            "aftermerge-clickhouse",
            "clickhouse-client",
            "--query",
            f"EXISTS DATABASE {database}",
        ],
        capture_output=True,
        text=True,
    )
    assert exists.stdout.strip() == "0"


def test_the_regression_reproduces_differentially() -> None:
    """The level-3 claim: the diff causes it, shown with a control.

    Correlation is an inference from production telemetry. This is an experiment.
    """
    from aftermerge.reproducer.differential import run_differential

    outcome = run_differential(
        RequestEnvelope.get("/orders", limit=50),
        good_ref="cbb4790",
        bad_ref=BAD_SHA,
        repo_root=ROOT,
        repeat=10,
    )

    assert outcome.good.db_spans_per_request == pytest.approx(2.0, rel=0.05)
    assert outcome.bad.db_spans_per_request == pytest.approx(51.0, rel=0.05)
    assert outcome.reproduced
