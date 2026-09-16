"""Generate a regression test, then accept it only if it passes the gate.

The gate runs as a **subprocess**, so each attempt already carries a real
`CompletedProcess` and the accepted one can be recorded as level-3 evidence
without re-running anything. Doing it in-process and then shelling out again
purely to satisfy `VerificationRepository` would cost two extra sandbox builds
for no added truth.

Giving up is a supported outcome. If no candidate discriminates between the two
commits, the incident keeps its report and gets no test, stated plainly. A
pipeline that always produces something is less trustworthy than one that can
say it could not.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aftermerge.testgen.context import TestContext
from aftermerge.testgen.generator import TestCandidate, TestGenerator
from aftermerge.testgen.writer import write

GATE_TIMEOUT_SECONDS = 3600

GateRunner = Callable[[Path, TestContext, Path], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Attempt:
    candidate: TestCandidate
    process: subprocess.CompletedProcess[str]
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.process.returncode == 0

    @property
    def summary(self) -> str:
        stated = str(self.payload.get("summary") or "").strip()
        if stated:
            return stated
        return f"gate exited {self.process.returncode} without reporting a result"


@dataclass(frozen=True)
class CertifyResult:
    attempts: tuple[Attempt, ...]
    accepted: Attempt | None
    test_path: Path | None

    @property
    def succeeded(self) -> bool:
        return self.accepted is not None

    @property
    def summary(self) -> str:
        if self.accepted is not None:
            return self.accepted.summary
        if not self.attempts:
            return "no candidate was generated"
        return (
            f"{len(self.attempts)} candidate(s) generated, none discriminated between "
            f"{self.attempts[-1].payload.get('bad_ref', 'the two commits')} and "
            f"{self.attempts[-1].payload.get('good_ref', 'its predecessor')}. "
            f"Last attempt: {self.attempts[-1].summary}"
        )


def _default_runner(
    test_path: Path, context: TestContext, repo_root: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "aftermerge.cli",
            "gate",
            "--test",
            str(test_path),
            "--good",
            context.baseline_version,
            "--bad",
            context.candidate_version,
            "--json",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=GATE_TIMEOUT_SECONDS,
    )


def certify(
    context: TestContext,
    generator: TestGenerator,
    *,
    repo_root: Path,
    max_attempts: int = 3,
    runner: GateRunner = _default_runner,
) -> CertifyResult:
    """Produce a gated regression test, or honestly report that none was found."""
    # Retrying a deterministic generator burns two sandbox builds to get the same
    # file back, so only a non-deterministic one is worth a second attempt.
    allowed = 1 if getattr(generator, "deterministic", False) else max_attempts

    attempts: list[Attempt] = []
    for _ in range(allowed):
        candidate = generator.generate(context)
        path = write(candidate, repo_root=repo_root)
        process = runner(path, context, repo_root)

        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            payload = {"parse_error": True, "stderr_tail": (process.stderr or "")[-800:]}

        attempt = Attempt(candidate=candidate, process=process, payload=payload)
        attempts.append(attempt)

        if attempt.passed:
            return CertifyResult(attempts=tuple(attempts), accepted=attempt, test_path=path)

        # A rejected candidate must not survive on disk. An ungated test sitting
        # in tests/regression/ is exactly the false assurance this design exists
        # to prevent.
        path.unlink(missing_ok=True)

    return CertifyResult(attempts=tuple(attempts), accepted=None, test_path=None)
