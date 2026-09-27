"""The propose/validate loop: accept, reject, retry, give up."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from aftermerge.patcher.fix import propose_fix
from aftermerge.patcher.proposer import PatchContext, Proposal

CTX = PatchContext(
    good_ref="cbb4790",
    bad_ref="8b4fd77",
    changed_files=("fixtures/shopdemo/orders/repository.py",),
    code_site="orders/repository.py",
    baseline_spans_per_request=2.0,
    candidate_spans_per_request=51.0,
    causing_diff="",
)

DIFF = (
    "diff --git a/fixtures/shopdemo/orders/repository.py b/fixtures/shopdemo/orders/repository.py\n"
    "--- a/fixtures/shopdemo/orders/repository.py\n"
    "+++ b/fixtures/shopdemo/orders/repository.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)


class _StubProposer:
    name = "stub"

    def __init__(self, deterministic: bool) -> None:
        self.deterministic = deterministic
        self.calls = 0
        self.last_feedback: str | None = None

    def propose(self, context: PatchContext, feedback: str | None = None) -> Proposal:
        self.calls += 1
        self.last_feedback = feedback
        return Proposal(files={"x": "y"}, strategy="repair", origin=self.name)


def runner_returning(code: int, payload: dict | None = None):
    def run(patch_path: Path, patch, repo_root: Path):
        return subprocess.CompletedProcess(
            args=["validate"],
            returncode=code,
            stdout=json.dumps(payload or {"summary": "stub", "checks": []}),
            stderr="",
        )

    return run


def patched_to_patch(monkeypatch):
    """Bypass git: the diff mechanics are covered in test_proposer."""
    from aftermerge.patcher import fix as fix_mod
    from aftermerge.patcher.patch import Patch

    monkeypatch.setattr(
        fix_mod,
        "to_patch",
        lambda proposal, base_ref, repo_root: Patch(
            diff=DIFF, strategy=proposal.strategy, origin=proposal.origin
        ),
    )


def test_an_accepted_fix_is_kept(tmp_path, monkeypatch) -> None:
    patched_to_patch(monkeypatch)
    outcome = propose_fix(CTX, _StubProposer(True), repo_root=tmp_path, runner=runner_returning(0))

    assert outcome.succeeded
    assert outcome.patch_path is not None and outcome.patch_path.exists()
    assert outcome.patch_path.read_text() == DIFF


def test_a_rejected_fix_is_deleted(tmp_path, monkeypatch) -> None:
    """A candidate nobody validated must not be left lying around."""
    patched_to_patch(monkeypatch)
    outcome = propose_fix(CTX, _StubProposer(True), repo_root=tmp_path, runner=runner_returning(1))

    assert not outcome.succeeded
    assert outcome.patch_path is None
    assert not (tmp_path / ".aftermerge" / "candidate.patch").exists()


def test_a_deterministic_proposer_is_not_retried(tmp_path, monkeypatch) -> None:
    """A retry costs three sandbox builds to regenerate the same diff."""
    patched_to_patch(monkeypatch)
    proposer = _StubProposer(True)
    propose_fix(CTX, proposer, repo_root=tmp_path, max_attempts=3, runner=runner_returning(1))
    assert proposer.calls == 1


def test_a_non_deterministic_proposer_retries(tmp_path, monkeypatch) -> None:
    patched_to_patch(monkeypatch)
    proposer = _StubProposer(False)
    outcome = propose_fix(
        CTX, proposer, repo_root=tmp_path, max_attempts=3, runner=runner_returning(1)
    )

    assert proposer.calls == 3
    assert len(outcome.attempts) == 3
    assert "none validated" in outcome.summary


def test_retrying_stops_on_the_first_acceptance(tmp_path, monkeypatch) -> None:
    patched_to_patch(monkeypatch)
    proposer = _StubProposer(False)
    codes = iter([1, 0, 0])

    def run(patch_path, patch, repo_root):
        return subprocess.CompletedProcess(
            args=["validate"], returncode=next(codes), stdout="{}", stderr=""
        )

    outcome = propose_fix(CTX, proposer, repo_root=tmp_path, max_attempts=3, runner=run)

    assert proposer.calls == 2
    assert outcome.succeeded


def test_an_unusable_proposal_counts_as_a_failed_attempt(tmp_path, monkeypatch) -> None:
    """Not a crash: a later attempt may do better, and the reason is reported."""
    from aftermerge.patcher import fix as fix_mod
    from aftermerge.patcher.patch import PatchRejected

    def boom(proposal, base_ref, repo_root):
        raise PatchRejected("proposal is identical to the commit it is meant to fix")

    monkeypatch.setattr(fix_mod, "to_patch", boom)
    outcome = propose_fix(
        CTX, _StubProposer(False), repo_root=tmp_path, max_attempts=2, runner=runner_returning(0)
    )

    assert not outcome.succeeded
    assert len(outcome.attempts) == 2
    assert "identical" in outcome.attempts[0].summary


def test_the_seed_actually_reaches_the_validate_command(tmp_path) -> None:
    """Guards a silent failure: a seed accepted, bound, and then dropped.

    Nothing else catches this. The signature type-checks, the partial binds, and
    a missing element in an argument list is invisible to mypy -- validation
    would run against the default dataset while reporting the requested one.
    """
    from aftermerge.patcher.fix import _default_runner
    from aftermerge.patcher.patch import Patch

    patch = Patch(diff=DIFF, strategy="revert", origin="test")
    captured: list[list[str]] = []

    class _Recorder:
        def __call__(self, args, **kwargs):
            captured.append(args)
            import subprocess as sp

            return sp.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    import aftermerge.patcher.fix as fix_mod

    original = fix_mod.subprocess.run
    fix_mod.subprocess.run = _Recorder()  # type: ignore[assignment]
    try:
        _default_runner(tmp_path / "c.patch", patch, tmp_path, seed="large")
        _default_runner(tmp_path / "c.patch", patch, tmp_path)
    finally:
        fix_mod.subprocess.run = original  # type: ignore[assignment]

    assert "--seed" in captured[0] and "large" in captured[0]
    assert "--seed" not in captured[1]
