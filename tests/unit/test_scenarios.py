"""Scenario files must stay consistent with the repository.

A scenario pins commit SHAs and file paths. If a branch is deleted, never
pushed, or a fixture moves, the scenario silently stops being reproducible --
these tests turn that into a failure instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = sorted((ROOT / "scenarios").glob("*.yaml"))


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _sha_exists(sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
        ).returncode
        == 0
    )


def test_at_least_one_scenario_exists() -> None:
    assert SCENARIOS


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario_paths_exist(path: Path) -> None:
    scenario = _load(path)
    for key in ("patch",):
        assert (ROOT / scenario["commits"][key]).is_file(), f"missing {key}"
    assert (ROOT / scenario["expect"]["root_cause_file"]).is_file()


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario_commits_are_reachable(path: Path) -> None:
    """Fails on a fresh clone if the regression branch was never pushed."""
    scenario = _load(path)
    for key in ("good_sha", "bad_sha"):
        sha = scenario["commits"][key]
        assert _sha_exists(sha), f"{key} {sha} is not reachable in this clone"
