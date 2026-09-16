"""Fixtures for generated regression tests.

A generated test is run twice -- once at the commit believed broken and once at
its predecessor -- so it must not name a commit itself. `AFTERMERGE_TEST_REF`
supplies it, which is what lets the same file discriminate between the two.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aftermerge.reproducer.sandbox import sandbox

ROOT = Path(__file__).resolve().parents[2]


HERE = Path(__file__).parent


def pytest_collection_modifyitems(items):
    """Mark generated regression tests slow -- they build containers.

    Scoped to this directory explicitly. `pytest_collection_modifyitems` is a
    session hook and receives *every* collected item regardless of which
    conftest defines it, so an unguarded loop here marks the entire suite slow
    and `make test` silently runs nothing.
    """
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.slow)


@pytest.fixture(scope="session")
def sandbox_under_test():
    ref = os.environ.get("AFTERMERGE_TEST_REF")
    if not ref:
        pytest.skip("set AFTERMERGE_TEST_REF to the commit under test")
    with sandbox(ref, repo_root=ROOT) as running:
        yield running
