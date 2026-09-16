"""A patch has to clear two cheap guards before anything runs it."""

from __future__ import annotations

import pytest

from aftermerge.patcher.patch import Patch, PatchRejected, enforce_scope, enforce_test_untouched

ALLOWED = frozenset({"fixtures/shopdemo/orders/repository.py"})
TEST_PATHS = frozenset({"tests/regression/test_orders_get_orders_regression.py"})


def diff_touching(*paths: str) -> str:
    return "\n".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new" for p in paths
    )


def patch_touching(*paths: str) -> Patch:
    return Patch(diff=diff_touching(*paths), strategy="repair", origin="test")


def test_touched_files_are_parsed_from_the_diff() -> None:
    patch = patch_touching("a.py", "b.py")
    assert patch.touched_files == {"a.py", "b.py"}


def test_a_patch_within_scope_is_accepted() -> None:
    enforce_scope(patch_touching("fixtures/shopdemo/orders/repository.py"), ALLOWED)


def test_a_patch_reaching_beyond_the_causing_diff_is_rejected() -> None:
    """A fix that wanders is a rewrite, and costs more to review than to write."""
    patch = patch_touching("fixtures/shopdemo/orders/repository.py", "src/aftermerge/cli.py")
    with pytest.raises(PatchRejected, match="did not"):
        enforce_scope(patch, ALLOWED)


def test_a_patch_editing_its_own_test_is_rejected() -> None:
    """The cheapest way for the pipeline to start lying, and one check prevents it."""
    patch = patch_touching("tests/regression/test_orders_get_orders_regression.py")
    with pytest.raises(PatchRejected, match="own examiner|examiner"):
        enforce_test_untouched(patch, TEST_PATHS)


def test_a_patch_editing_code_and_test_together_is_still_rejected() -> None:
    patch = patch_touching(
        "fixtures/shopdemo/orders/repository.py",
        "tests/regression/test_orders_get_orders_regression.py",
    )
    with pytest.raises(PatchRejected):
        enforce_test_untouched(patch, TEST_PATHS)


def test_an_unknown_strategy_is_rejected() -> None:
    """A reviewer must be told whether this reverts or repairs."""
    with pytest.raises(PatchRejected, match="strategy"):
        Patch(diff=diff_touching("a.py"), strategy="improvise", origin="test")


def test_an_empty_patch_is_rejected() -> None:
    with pytest.raises(PatchRejected, match="empty"):
        Patch(diff="   ", strategy="repair", origin="test")
