"""Putting a generated test on disk."""

from __future__ import annotations

from pathlib import Path

from aftermerge.testgen.generator import TestCandidate

REGRESSION_DIR = Path("tests") / "regression"


def write(candidate: TestCandidate, *, repo_root: Path, directory: Path = REGRESSION_DIR) -> Path:
    """Write a candidate and return its path.

    Overwrites an existing file for the same incident on purpose: a rejected
    candidate should not linger next to the one that replaced it, since a stale
    test nobody gated is exactly the thing this design is trying to avoid.
    """
    target_dir = repo_root / directory
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{candidate.module_name}.py"
    path.write_text(candidate.source)
    return path
