"""Validating a generated test by running it at both commits.

This is what makes generation trustworthy. A test that passes everywhere proves
nothing; a test that fails everywhere proves nothing. Only one that *fails on the
bad commit and passes on the good one* has demonstrated that it encodes the
regression -- and that is established by two recorded exit codes, not by the
generator's opinion of its own output.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: pytest's exit codes. Only 1 means "tests ran and failed". Everything else
#: non-zero means the run never got that far -- a syntax error, a usage mistake,
#: nothing collected. That distinction is load-bearing: a test file with a typo
#: also "fails" at the bad commit, and accepting any non-zero code there would
#: certify broken tests as regression coverage.
PYTEST_ALL_PASSED = 0
PYTEST_TESTS_FAILED = 1
PYTEST_NO_TESTS = 5

#: Not a pytest code. Marks a side that was deliberately not attempted, so the
#: report says "not run" rather than implying the test failed there.
NOT_RUN = -1

TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class GateRun:
    ref: str
    expectation: str
    exit_code: int
    stdout_tail: str

    @property
    def satisfied(self) -> bool:
        if self.expectation == "fail":
            return self.exit_code == PYTEST_TESTS_FAILED
        return self.exit_code == PYTEST_ALL_PASSED

    @property
    def explanation(self) -> str:
        if self.exit_code == NOT_RUN:
            return f"{self.ref}: not run (the other side had already failed the gate)"
        if self.satisfied:
            return f"{self.ref}: {self.expectation}ed as required (exit {self.exit_code})"
        if self.exit_code == PYTEST_NO_TESTS:
            return f"{self.ref}: no tests were collected (exit 5)"
        if self.expectation == "fail" and self.exit_code == PYTEST_ALL_PASSED:
            return f"{self.ref}: passed, but must fail -- the test does not detect the regression"
        if self.expectation == "fail":
            return (
                f"{self.ref}: exited {self.exit_code} rather than failing a test; "
                "the run broke instead of detecting anything"
            )
        return f"{self.ref}: expected to pass, exited {self.exit_code}"


@dataclass(frozen=True)
class GateResult:
    test_path: Path
    at_bad: GateRun
    at_good: GateRun

    @property
    def passed(self) -> bool:
        return self.at_bad.satisfied and self.at_good.satisfied

    @property
    def reasons(self) -> tuple[str, ...]:
        return (self.at_bad.explanation, self.at_good.explanation)

    @property
    def summary(self) -> str:
        if self.passed:
            return (
                f"{self.test_path.name} fails at {self.at_bad.ref} and passes at "
                f"{self.at_good.ref}; it discriminates between the two commits."
            )
        faults = [r for r in self.reasons if "as required" not in r and "not run" not in r]
        return f"{self.test_path.name} does not discriminate: " + "; ".join(faults)


def _run_at(test_path: Path, ref: str, expectation: str, *, repo_root: Path) -> GateRun:
    process = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "slow", "-q", str(test_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env={**os.environ, "AFTERMERGE_TEST_REF": ref},
    )
    return GateRun(
        ref=ref,
        expectation=expectation,
        exit_code=process.returncode,
        stdout_tail=(process.stdout or "")[-2000:],
    )


def run_gate(test_path: Path, *, good_ref: str, bad_ref: str, repo_root: Path) -> GateResult:
    """Run the test at both commits and report whether it discriminates.

    The bad commit is tried first. A candidate that cannot detect the regression
    is the common failure, and finding that out early saves a sandbox build.
    """
    at_bad = _run_at(test_path, bad_ref, "fail", repo_root=repo_root)
    if not at_bad.satisfied:
        # Still record a good-side result so the report is complete, but do not
        # spend a second sandbox on a candidate that has already lost.
        return GateResult(
            test_path=test_path,
            at_bad=at_bad,
            at_good=GateRun(ref=good_ref, expectation="pass", exit_code=NOT_RUN, stdout_tail=""),
        )

    at_good = _run_at(test_path, good_ref, "pass", repo_root=repo_root)
    return GateResult(test_path=test_path, at_bad=at_bad, at_good=at_good)
