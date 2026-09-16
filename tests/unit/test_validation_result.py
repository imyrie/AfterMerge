"""How individual checks combine into a verdict."""

from __future__ import annotations

from aftermerge.patcher.validate import Check, ValidationResult


def result(*checks: Check) -> ValidationResult:
    return ValidationResult(patched_sha="abc1234", strategy="revert", checks=checks)


PASSED = Check("patch_regression_test", "passed", "ok")
FAILED = Check("patch_response_equivalence", "failed", "body length 17503 became 900")
SKIPPED = Check("patch_suite", "skipped", "no suite command configured")


def test_all_passing_validates() -> None:
    assert result(PASSED, Check("patch_work_restored", "passed", "2.0 vs 2.0")).passed


def test_any_failure_blocks() -> None:
    assert not result(PASSED, FAILED).passed
    assert "body length" in result(PASSED, FAILED).summary


def test_a_skipped_check_does_not_block() -> None:
    """Absent evidence is not counted as failure..."""
    assert result(PASSED, SKIPPED).passed


def test_a_skipped_check_is_named_in_the_summary() -> None:
    """...but it is not quietly counted as success either."""
    assert "skipped: patch_suite" in result(PASSED, SKIPPED).summary


def test_nothing_but_skips_does_not_validate() -> None:
    """A fix with no positive evidence has not been validated."""
    assert not result(SKIPPED).passed


def test_the_strategy_is_stated() -> None:
    assert "(revert)" in result(PASSED).summary
