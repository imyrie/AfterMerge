"""The certify loop: accept, reject, retry, and give up."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from aftermerge.testgen.certify import certify
from aftermerge.testgen.context import TestContext
from aftermerge.testgen.generator import TemplateGenerator, TestCandidate

CTX = TestContext(
    service="orders",
    route="GET /orders",
    baseline_version="cbb4790",
    candidate_version="8b4fd77",
    method="GET",
    path="/orders",
    query={"limit": "50"},
    baseline_spans_per_request=2.0,
    candidate_spans_per_request=51.0,
    code_site="orders/repository.py",
)


def runner_returning(exit_code: int, payload: dict | None = None):
    def run(test_path: Path, context: TestContext, repo_root: Path):
        return subprocess.CompletedProcess(
            args=["gate", str(test_path)],
            returncode=exit_code,
            stdout=json.dumps(payload or {"summary": "stub", "reasons": []}),
            stderr="",
        )

    return run


class _Counting:
    """A non-deterministic generator, so retries are allowed."""

    name = "counting"
    deterministic = False

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, context: TestContext) -> TestCandidate:
        self.calls += 1
        return TestCandidate(
            module_name=f"test_attempt_{self.calls}",
            source="def test_x():\n    assert True\n",
            generated_by=self.name,
            rationale="stub",
        )


def test_an_accepted_candidate_is_kept_on_disk(tmp_path) -> None:
    outcome = certify(CTX, TemplateGenerator(), repo_root=tmp_path, runner=runner_returning(0))

    assert outcome.succeeded
    assert outcome.test_path is not None and outcome.test_path.exists()


def test_a_rejected_candidate_is_deleted(tmp_path) -> None:
    """An ungated test left in tests/regression/ is false assurance."""
    outcome = certify(CTX, TemplateGenerator(), repo_root=tmp_path, runner=runner_returning(1))

    assert not outcome.succeeded
    assert outcome.test_path is None
    assert list((tmp_path / "tests" / "regression").glob("*.py")) == []


def test_a_deterministic_generator_is_not_retried(tmp_path) -> None:
    """Retrying would spend two sandbox builds to regenerate the same file."""
    outcome = certify(
        CTX, TemplateGenerator(), repo_root=tmp_path, max_attempts=3, runner=runner_returning(1)
    )
    assert len(outcome.attempts) == 1


def test_a_non_deterministic_generator_retries_to_the_limit(tmp_path) -> None:
    generator = _Counting()
    outcome = certify(
        CTX, generator, repo_root=tmp_path, max_attempts=3, runner=runner_returning(1)
    )

    assert generator.calls == 3
    assert len(outcome.attempts) == 3
    assert not outcome.succeeded


def test_retrying_stops_as_soon_as_one_is_accepted(tmp_path) -> None:
    generator = _Counting()
    codes = iter([1, 0, 0])

    def run(test_path, context, repo_root):
        return subprocess.CompletedProcess(
            args=["gate"], returncode=next(codes), stdout="{}", stderr=""
        )

    outcome = certify(CTX, generator, repo_root=tmp_path, max_attempts=3, runner=run)

    assert generator.calls == 2
    assert outcome.succeeded


def test_giving_up_is_reported_plainly(tmp_path) -> None:
    """A pipeline that always produces something is less trustworthy."""
    outcome = certify(CTX, TemplateGenerator(), repo_root=tmp_path, runner=runner_returning(1))
    assert "none discriminated" in outcome.summary


def test_unparseable_gate_output_does_not_crash(tmp_path) -> None:
    def run(test_path, context, repo_root):
        return subprocess.CompletedProcess(
            args=["gate"], returncode=2, stdout="not json", stderr="boom"
        )

    outcome = certify(CTX, TemplateGenerator(), repo_root=tmp_path, runner=run)

    assert not outcome.succeeded
    assert outcome.attempts[0].payload["parse_error"] is True
