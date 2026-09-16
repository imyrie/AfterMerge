"""Proving a candidate fix, before anyone is asked to review it.

Four checks. The last two are the ones that matter, because the first two can
both pass on a patch that is quietly wrong.

`validate` is a single subprocess boundary rather than one per check. Four
separate commands would each rebuild sandboxes for evidence that can be gathered
from two, and the extra rows would cost roughly four minutes to buy nothing: the
per-check results are recorded in the verification's metrics and rendered in the
report either way.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aftermerge.patcher.equivalence import compare, snapshot
from aftermerge.patcher.patch import Patch, enforce_scope, enforce_test_untouched
from aftermerge.patcher.tree import patched_commit
from aftermerge.reproducer.envelope import RequestEnvelope
from aftermerge.reproducer.replay import replay
from aftermerge.reproducer.sandbox import sandbox

PYTEST_TESTS_FAILED = 1
TIMEOUT_SECONDS = 1800

#: How much more database work than the known-good build a fix may do before the
#: regression counts as un-fixed rather than merely improved.
WORK_TOLERANCE = 1.5


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def blocking(self) -> bool:
        """A skipped check is not a failure, but it is not evidence either."""
        return self.status == "failed"


@dataclass(frozen=True)
class ValidationResult:
    patched_sha: str
    strategy: str
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return any(c.passed for c in self.checks) and not any(c.blocking for c in self.checks)

    @property
    def summary(self) -> str:
        if self.passed:
            skipped = [c.name for c in self.checks if c.status == "skipped"]
            note = f" ({len(skipped)} check(s) skipped: {', '.join(skipped)})" if skipped else ""
            return f"candidate fix {self.patched_sha} ({self.strategy}) passed validation{note}"
        failures = [f"{c.name}: {c.detail}" for c in self.checks if c.blocking]
        return f"candidate fix {self.patched_sha} rejected -- " + "; ".join(failures)

    def as_dict(self) -> dict[str, Any]:
        return {
            "patched_sha": self.patched_sha,
            "strategy": self.strategy,
            "passed": self.passed,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks
            ],
            "summary": self.summary,
        }


def _pytest(
    args: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env={**os.environ, **(env or {})},
    )


def _check_regression_test(test_path: Path, patched_sha: str, repo_root: Path) -> Check:
    """The test that failed on the bad commit must now pass."""
    process = _pytest(
        ["-m", "slow", "-q", str(test_path)],
        cwd=repo_root,
        env={"AFTERMERGE_TEST_REF": patched_sha},
    )
    if process.returncode == 0:
        return Check("patch_regression_test", "passed", f"{test_path.name} passes at {patched_sha}")
    if process.returncode == PYTEST_TESTS_FAILED:
        return Check("patch_regression_test", "failed", "the regression is still present")
    return Check(
        "patch_regression_test",
        "failed",
        f"the run broke rather than reporting a result (exit {process.returncode})",
    )


def _check_suite(suite_command: list[str] | None, repo_root: Path) -> Check:
    """The project's own tests, when it has any.

    shopdemo has none, so for slice 0's fixture this is skipped and stated as
    skipped. Pretending an absent suite is a passing suite would be the kind of
    quiet overstatement this project exists to avoid.
    """
    if not suite_command:
        return Check(
            "patch_suite",
            "skipped",
            "no suite command configured for this scenario; response equivalence is the oracle",
        )
    process = subprocess.run(
        suite_command, cwd=repo_root, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
    )
    if process.returncode == 0:
        return Check("patch_suite", "passed", "project test suite passes")
    return Check("patch_suite", "failed", f"project test suite exited {process.returncode}")


def validate(
    patch: Patch,
    *,
    envelope: RequestEnvelope,
    good_ref: str,
    bad_ref: str,
    test_path: Path,
    repo_root: Path,
    allowed_files: frozenset[str],
    normalisations: tuple[str, ...] = (),
    suite_command: list[str] | None = None,
    repeat: int = 10,
) -> ValidationResult:
    """Apply a patch, then establish that it fixes the fault and changes nothing else."""
    # Cheap guards first. Neither needs a container, and both are disqualifying.
    enforce_scope(patch, allowed_files)
    enforce_test_untouched(patch, frozenset({str(test_path)}))

    with patched_commit(patch, base_ref=bad_ref, repo_root=repo_root) as patched_sha:
        checks: list[Check] = [
            _check_regression_test(test_path, patched_sha, repo_root),
            _check_suite(suite_command, repo_root),
        ]

        with sandbox(good_ref, repo_root=repo_root) as good_box:
            good_work = replay(good_box, envelope, repeat=repeat)
            good_response = snapshot(good_box, envelope)

        with sandbox(patched_sha, repo_root=repo_root) as patched_box:
            patched_work = replay(patched_box, envelope, repeat=repeat)
            patched_response = snapshot(patched_box, envelope)

        ceiling = max(
            good_work.db_spans_per_request * WORK_TOLERANCE, good_work.db_spans_per_request + 1
        )
        if patched_work.db_spans_per_request <= ceiling:
            checks.append(
                Check(
                    "patch_work_restored",
                    "passed",
                    f"{good_work.db_spans_per_request:.1f} vs "
                    f"{patched_work.db_spans_per_request:.1f} database operations per request",
                )
            )
        else:
            checks.append(
                Check(
                    "patch_work_restored",
                    "failed",
                    f"still doing {patched_work.db_spans_per_request:.1f} operations per request "
                    f"against a baseline of {good_work.db_spans_per_request:.1f}",
                )
            )

        equivalence = compare(good_response, patched_response, normalisations=normalisations)
        checks.append(
            Check(
                "patch_response_equivalence",
                "passed" if equivalence.equivalent else "failed",
                equivalence.summary,
            )
        )

        return ValidationResult(
            patched_sha=patched_sha, strategy=patch.strategy, checks=tuple(checks)
        )
