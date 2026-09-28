"""Data quality checks, including the defect that motivated them."""

from __future__ import annotations

from pathlib import Path

from aftermerge.dataquality.checks import (
    FAILED,
    PASSED,
    SKIPPED,
    CheckResult,
    DataQualityReport,
    filter_literals,
)
from aftermerge.dataquality.runner import (
    check_filter_literals_match_data,
    check_freshness,
    check_server_span_completeness,
    check_status_code_domain,
    check_version_cardinality,
    run_all,
)


class FakeCH:
    """Returns canned rows per query fragment."""

    def __init__(self, answers: dict[str, list[tuple]]) -> None:
        self._answers = answers

    def query(self, sql: str, parameters: dict | None = None):
        for fragment, rows in self._answers.items():
            if fragment in sql:
                return type("R", (), {"result_rows": rows})()
        return type("R", (), {"result_rows": []})()


# --- the catalog-literal check: the reason this module exists -----------------


def test_filter_literals_are_extracted_from_the_catalog(tmp_path: Path) -> None:
    (tmp_path / "q.sql").write_text(
        "SELECT countIf(StatusCode = 'STATUS_CODE_ERROR') FROM otel_traces\n"
        "WHERE SpanKind = 'Server'\n"
    )
    assert filter_literals(tmp_path, "StatusCode") == {"STATUS_CODE_ERROR"}
    assert filter_literals(tmp_path, "SpanKind") == {"Server"}


def test_a_literal_that_never_occurs_is_caught(tmp_path: Path) -> None:
    """The exact historical defect.

    `StatusCode = 'STATUS_CODE_ERROR'` matched nothing because ClickHouse writes
    'Error'. Nothing failed -- the error count simply read zero, for every
    measurement in every slice, and zero is a plausible error count.
    """
    (tmp_path / "q.sql").write_text("SELECT countIf(StatusCode = 'STATUS_CODE_ERROR') FROM t")
    ch = FakeCH({"DISTINCT StatusCode": [("Unset",), ("Error",)]})

    result = check_filter_literals_match_data(ch, queries_dir=tmp_path)

    assert result.status == FAILED
    assert "STATUS_CODE_ERROR" in result.detail
    assert "returns zero rather than failing" in result.detail


def test_literals_that_do_occur_pass(tmp_path: Path) -> None:
    (tmp_path / "q.sql").write_text("SELECT countIf(StatusCode = 'Error') FROM t")
    ch = FakeCH({"DISTINCT StatusCode": [("Unset",), ("Error",)]})
    assert check_filter_literals_match_data(ch, queries_dir=tmp_path).status == PASSED


def test_no_watched_literals_skips_rather_than_passes(tmp_path: Path) -> None:
    (tmp_path / "q.sql").write_text("SELECT count() FROM t")
    ch = FakeCH({})
    assert check_filter_literals_match_data(ch, queries_dir=tmp_path).status == SKIPPED


# --- the rest ----------------------------------------------------------------


def test_stale_telemetry_fails() -> None:
    ch = FakeCH({"dateDiff": [(900,)]})
    result = check_freshness(ch, max_staleness_minutes=240)
    assert result.status == FAILED
    assert result.measured["age_minutes"] == 900


def test_fresh_telemetry_passes() -> None:
    ch = FakeCH({"dateDiff": [(5,)]})
    assert check_freshness(ch, max_staleness_minutes=240).status == PASSED


def test_an_unknown_status_value_fails() -> None:
    """Means the exporter's vocabulary moved and old filters now match nothing."""
    ch = FakeCH({"DISTINCT StatusCode": [("Unset",), ("STATUS_CODE_ERROR",)]})
    result = check_status_code_domain(ch)
    assert result.status == FAILED
    assert "STATUS_CODE_ERROR" in result.detail


def test_a_single_version_cannot_support_a_comparison() -> None:
    ch = FakeCH({"uniqExact": [(1,)]})
    result = check_version_cardinality(ch, lookback_minutes=240)
    assert result.status == FAILED
    assert "not possible" in result.detail


def test_two_versions_pass() -> None:
    ch = FakeCH({"uniqExact": [(2,)]})
    assert check_version_cardinality(ch, lookback_minutes=240).status == PASSED


def test_missing_urls_fail_because_replay_would_target_the_wrong_thing() -> None:
    ch = FakeCH({"http.url": [(100, 40)]})
    result = check_server_span_completeness(ch, lookback_minutes=240)
    assert result.status == FAILED
    assert result.measured["ratio"] == 0.4


def test_complete_urls_pass() -> None:
    ch = FakeCH({"http.url": [(100, 100)]})
    assert check_server_span_completeness(ch, lookback_minutes=240).status == PASSED


# --- report semantics --------------------------------------------------------


def test_a_failure_fails_the_report() -> None:
    report = DataQualityReport(
        results=(
            CheckResult("a", PASSED, ""),
            CheckResult("b", FAILED, "broken"),
        )
    )
    assert not report.passed
    assert "broken" in report.summary


def test_skips_alone_do_not_count_as_passing() -> None:
    """A run where nothing could be checked has proved nothing."""
    report = DataQualityReport(results=(CheckResult("a", SKIPPED, "unreachable"),))
    assert not report.passed
    assert "no check produced evidence" in report.summary


def test_a_pass_with_skips_still_passes() -> None:
    report = DataQualityReport(
        results=(
            CheckResult("a", PASSED, ""),
            CheckResult("b", SKIPPED, "unreachable"),
        )
    )
    assert report.passed


def test_unreachable_stores_skip_instead_of_crashing() -> None:
    report = run_all(ch=None, session=None)
    assert not report.passed
    assert all(r.status == SKIPPED for r in report.results)
