"""Gate semantics: which exit codes count as evidence."""

from __future__ import annotations

from pathlib import Path

from aftermerge.testgen.gate import NOT_RUN, GateResult, GateRun


def run(ref: str, expectation: str, exit_code: int) -> GateRun:
    return GateRun(ref=ref, expectation=expectation, exit_code=exit_code, stdout_tail="")


def result(bad_code: int, good_code: int) -> GateResult:
    return GateResult(
        test_path=Path("tests/regression/test_x.py"),
        at_bad=run("8b4fd77", "fail", bad_code),
        at_good=run("cbb4790", "pass", good_code),
    )


def test_the_discriminating_case_passes() -> None:
    assert result(bad_code=1, good_code=0).passed


def test_a_test_that_passes_everywhere_is_rejected() -> None:
    """It proves nothing about the regression."""
    outcome = result(bad_code=0, good_code=0)
    assert not outcome.passed
    assert "does not detect the regression" in outcome.at_bad.explanation


def test_a_test_that_fails_everywhere_is_rejected() -> None:
    outcome = result(bad_code=1, good_code=1)
    assert not outcome.passed
    assert "expected to pass" in outcome.at_good.explanation


def test_a_broken_test_file_does_not_count_as_detection() -> None:
    """The load-bearing distinction.

    A file with a syntax error also 'fails' at the bad commit. Accepting any
    non-zero exit there would certify broken tests as regression coverage, so
    only pytest's code 1 -- tests ran and failed -- satisfies the bad side.
    """
    for broken in (2, 3, 4):
        outcome = result(bad_code=broken, good_code=0)
        assert not outcome.passed, f"exit {broken} must not satisfy the bad side"
        assert "broke instead of detecting" in outcome.at_bad.explanation


def test_collecting_no_tests_is_reported_as_such() -> None:
    outcome = result(bad_code=5, good_code=0)
    assert not outcome.passed
    assert "no tests were collected" in outcome.at_bad.explanation


def test_a_side_that_was_not_run_says_so() -> None:
    """Rather than implying the test failed there."""
    outcome = result(bad_code=0, good_code=NOT_RUN)
    assert "not run" in outcome.at_good.explanation
    assert "not run" not in outcome.summary


def test_summary_names_only_the_actual_faults() -> None:
    outcome = result(bad_code=0, good_code=NOT_RUN)
    assert "does not discriminate" in outcome.summary
    assert "as required" not in outcome.summary
