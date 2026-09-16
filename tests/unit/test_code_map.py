"""Diff paths and span paths must be expressed in the same coordinates."""

from __future__ import annotations

from pathlib import Path

import pytest

from aftermerge.investigator import code_map

REPO = Path(__file__).resolve().parents[2]


def test_repo_paths_are_mapped_onto_span_code_sites() -> None:
    """Git says fixtures/shopdemo/orders/..., spans say orders/... .

    If this mapping drifts, correlation silently attributes nothing and the
    investigation degrades to a timing coincidence.
    """
    changes = code_map.changed_files("cbb4790", "8b4fd77", repo_root=REPO)

    assert len(changes) == 1
    (change,) = changes
    assert change.repo_path == "fixtures/shopdemo/orders/repository.py"
    assert change.source_path == "orders/repository.py"
    assert change.churn == 30


def test_files_outside_the_deployed_tree_have_no_source_path() -> None:
    changes = code_map.changed_files(
        "cbb4790", "8b4fd77", repo_root=REPO, source_prefix="some/other/root"
    )
    assert all(c.source_path is None for c in changes)


def test_commit_metadata_is_read_from_git() -> None:
    info = code_map.commit_info("8b4fd77", repo_root=REPO)
    assert info.sha == "8b4fd77"
    assert info.subject == "Simplify order item lookup"


def test_unreachable_commit_raises_rather_than_returning_nothing() -> None:
    """Silently returning [] would read as 'the deploy changed nothing'."""
    with pytest.raises(code_map.GitUnavailable):
        code_map.changed_files("cbb4790", "0000000deadbeef", repo_root=REPO)
