"""A retry that is not told why it failed is just a re-roll."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from aftermerge.patcher.fix import propose_fix
from aftermerge.patcher.proposer import PatchContext, Proposal
from aftermerge.testgen.certify import certify
from aftermerge.testgen.context import TestContext
from aftermerge.testgen.generator import AnthropicGenerator, TemplateGenerator, TestCandidate

TEST_CTX = TestContext(
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

PATCH_CTX = PatchContext(
    good_ref="cbb4790",
    bad_ref="8b4fd77",
    changed_files=("fixtures/shopdemo/orders/repository.py",),
    code_site="orders/repository.py",
    baseline_spans_per_request=2.0,
    candidate_spans_per_request=51.0,
    causing_diff="",
)


class _RecordingGenerator:
    name = "recording"
    deterministic = False

    def __init__(self) -> None:
        self.feedback: list[str | None] = []

    def generate(self, context, feedback=None) -> TestCandidate:
        self.feedback.append(feedback)
        return TestCandidate(
            module_name="test_stub",
            source="def test_x():\n    assert True\n",
            generated_by=self.name,
            rationale="stub",
        )


class _RecordingProposer:
    name = "recording"
    deterministic = False

    def __init__(self) -> None:
        self.feedback: list[str | None] = []

    def propose(self, context, feedback=None) -> Proposal:
        self.feedback.append(feedback)
        return Proposal(files={"x": "y"}, strategy="repair", origin=self.name)


def _gate_runner(payload: dict):
    def run(test_path: Path, context, repo_root: Path):
        return subprocess.CompletedProcess(
            args=["gate"], returncode=1, stdout=json.dumps(payload), stderr=""
        )

    return run


def _validate_runner(payload: dict):
    def run(patch_path: Path, patch, repo_root: Path):
        return subprocess.CompletedProcess(
            args=["validate"], returncode=1, stdout=json.dumps(payload), stderr=""
        )

    return run


def test_the_first_attempt_gets_no_feedback(tmp_path) -> None:
    generator = _RecordingGenerator()
    certify(
        TEST_CTX,
        generator,
        repo_root=tmp_path,
        max_attempts=2,
        runner=_gate_runner({"reasons": ["8b4fd77: passed, but must fail"]}),
    )
    assert generator.feedback[0] is None


def test_a_rejected_test_reports_why_to_the_next_attempt(tmp_path) -> None:
    generator = _RecordingGenerator()
    certify(
        TEST_CTX,
        generator,
        repo_root=tmp_path,
        max_attempts=2,
        runner=_gate_runner({"reasons": ["8b4fd77: passed, but must fail"]}),
    )

    assert len(generator.feedback) == 2
    assert "passed, but must fail" in (generator.feedback[1] or "")


def test_a_rejected_patch_reports_only_the_failed_checks(tmp_path, monkeypatch) -> None:
    """Passing checks are not feedback; repeating them would bury the signal."""
    from aftermerge.patcher import fix as fix_mod
    from aftermerge.patcher.patch import Patch

    monkeypatch.setattr(
        fix_mod,
        "to_patch",
        lambda proposal, base_ref, repo_root: Patch(
            diff="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
            strategy=proposal.strategy,
            origin=proposal.origin,
        ),
    )
    proposer = _RecordingProposer()
    propose_fix(
        PATCH_CTX,
        proposer,
        repo_root=tmp_path,
        max_attempts=2,
        runner=_validate_runner(
            {
                "checks": [
                    {"name": "patch_regression_test", "status": "passed", "detail": "fine"},
                    {
                        "name": "patch_response_equivalence",
                        "status": "failed",
                        "detail": "body length 17503 became 6320",
                    },
                ]
            }
        ),
    )

    second = proposer.feedback[1] or ""
    assert "patch_response_equivalence" in second
    assert "17503 became 6320" in second
    assert "patch_regression_test" not in second


def test_the_deterministic_generator_ignores_feedback() -> None:
    """Its output cannot change, so feedback must not appear to influence it."""
    generator = TemplateGenerator()
    without = generator.generate(TEST_CTX).source
    with_feedback = generator.generate(TEST_CTX, "it was rejected because X").source
    assert without == with_feedback


class _FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.prompts.append(kwargs["messages"][0]["content"])
                return type(
                    "R", (), {"content": [_FakeBlock("def test_x():\n    assert True\n")]}
                )()

        self.messages = _Messages()


def test_the_model_prompt_carries_the_rejection_reason() -> None:
    client = _FakeClient()
    AnthropicGenerator(client).generate(TEST_CTX, "- 8b4fd77: passed, but must fail")

    prompt = client.prompts[0]
    assert "REJECTED" in prompt
    assert "passed, but must fail" in prompt
    assert "Do not repeat the same approach" in prompt


def test_a_first_attempt_prompt_has_no_retry_preamble() -> None:
    client = _FakeClient()
    AnthropicGenerator(client).generate(TEST_CTX)
    assert "REJECTED" not in client.prompts[0]
