"""Propose a fix, and keep it only if validation accepts it.

The same shape as `certify`: the loop never decides whether a patch is good. It
asks `aftermerge validate`, which runs as a subprocess, and takes the exit code
as the answer. A rejected candidate is deleted rather than left on disk.

Giving up is a supported outcome. If nothing validates, the incident keeps its
report and its regression test and gets no patch -- which is a better result than
a plausible diff nobody checked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aftermerge.patcher.patch import Patch, PatchRejected
from aftermerge.patcher.proposer import PatchContext, PatchProposer, to_patch

CANDIDATE_DIR = Path(".aftermerge")
CANDIDATE_NAME = "candidate.patch"
VALIDATE_TIMEOUT_SECONDS = 3600

ValidateRunner = Callable[[Path, Patch, Path], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class FixAttempt:
    patch: Patch
    process: subprocess.CompletedProcess[str]
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.process.returncode == 0

    @property
    def summary(self) -> str:
        stated = str(self.payload.get("summary") or self.payload.get("rejected") or "").strip()
        return stated or f"validation exited {self.process.returncode} without reporting a result"


@dataclass(frozen=True)
class FixResult:
    attempts: tuple[FixAttempt, ...]
    accepted: FixAttempt | None
    patch_path: Path | None

    @property
    def succeeded(self) -> bool:
        return self.accepted is not None

    @property
    def summary(self) -> str:
        if self.accepted is not None:
            return self.accepted.summary
        if not self.attempts:
            return "no candidate fix was proposed"
        return (
            f"{len(self.attempts)} candidate fix(es) proposed, none validated. "
            f"Last attempt: {self.attempts[-1].summary}"
        )


def _default_runner(
    patch_path: Path, patch: Patch, repo_root: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "aftermerge.cli",
            "validate",
            "--patch",
            str(patch_path),
            "--strategy",
            patch.strategy,
            "--origin",
            patch.origin,
            "--json",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=VALIDATE_TIMEOUT_SECONDS,
    )


def propose_fix(
    context: PatchContext,
    proposer: PatchProposer,
    *,
    repo_root: Path,
    max_attempts: int = 3,
    runner: ValidateRunner = _default_runner,
) -> FixResult:
    """Produce a validated fix, or honestly report that none was found."""
    # Retrying a deterministic proposer regenerates the same diff at the cost of
    # three sandbox builds, so only a non-deterministic one gets further goes.
    allowed = 1 if getattr(proposer, "deterministic", False) else max_attempts

    target_dir = repo_root / CANDIDATE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    patch_path = target_dir / CANDIDATE_NAME

    attempts: list[FixAttempt] = []
    for _ in range(allowed):
        proposal = proposer.propose(context)
        try:
            patch = to_patch(proposal, base_ref=context.bad_ref, repo_root=repo_root)
        except PatchRejected as exc:
            # A proposal that produces no usable diff is a failed attempt, not a
            # crash: the next attempt may do better, and the last one is reported.
            attempts.append(
                FixAttempt(
                    patch=Patch(diff="# unusable\n", strategy="repair", origin=proposer.name),
                    process=subprocess.CompletedProcess(
                        args=["propose"], returncode=1, stdout="", stderr=str(exc)
                    ),
                    payload={"rejected": str(exc)},
                )
            )
            continue

        patch_path.write_text(patch.diff)
        process = runner(patch_path, patch, repo_root)
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            payload = {"parse_error": True, "stderr_tail": (process.stderr or "")[-800:]}

        attempt = FixAttempt(patch=patch, process=process, payload=payload)
        attempts.append(attempt)

        if attempt.accepted:
            return FixResult(attempts=tuple(attempts), accepted=attempt, patch_path=patch_path)

        patch_path.unlink(missing_ok=True)

    return FixResult(attempts=tuple(attempts), accepted=None, patch_path=None)
