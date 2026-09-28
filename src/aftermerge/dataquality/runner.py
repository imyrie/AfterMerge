"""Running the data quality checks against the warehouse and the audit store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from aftermerge.dataquality.checks import (
    FAILED,
    KNOWN_STATUS_CODES,
    PASSED,
    SKIPPED,
    WATCHED_FILTER_COLUMNS,
    CheckResult,
    DataQualityReport,
    filter_literals,
)

QUERIES_DIR = Path("src/aftermerge/telemetry/queries")
DEFAULT_LOOKBACK_MINUTES = 240
DEFAULT_MAX_STALENESS_MINUTES = 240
MIN_URL_COMPLETENESS = 0.99


def _scalar(ch: Any, sql: str, **params: Any) -> Any:
    rows = ch.query(sql, parameters=params).result_rows
    return rows[0][0] if rows and rows[0] else None


def check_freshness(ch: Any, *, max_staleness_minutes: int) -> CheckResult:
    """Stale telemetry means the analysis describes a past the deploy has left."""
    age = _scalar(
        ch,
        "SELECT dateDiff('minute', max(Timestamp), now()) FROM otel_traces",
    )
    if age is None:
        return CheckResult("freshness", SKIPPED, "otel_traces is empty")
    if age > max_staleness_minutes:
        return CheckResult(
            "freshness",
            FAILED,
            f"newest span is {age} minutes old, beyond the {max_staleness_minutes} minute window",
            {"age_minutes": int(age)},
        )
    return CheckResult(
        "freshness", PASSED, f"newest span is {age} minutes old", {"age_minutes": int(age)}
    )


def check_filter_literals_match_data(ch: Any, *, queries_dir: Path) -> CheckResult:
    """Every literal the catalog filters on must actually occur in the data.

    This is the check that would have caught `StatusCode = 'STATUS_CODE_ERROR'`.
    A filter that matches nothing does not raise; it silently returns zero, and
    zero is a plausible number for an error count.
    """
    missing: list[str] = []
    checked = 0
    for column in WATCHED_FILTER_COLUMNS:
        literals = filter_literals(queries_dir, column)
        if not literals:
            continue
        observed = {
            str(row[0])
            for row in ch.query(f"SELECT DISTINCT {column} FROM otel_traces").result_rows
        }
        for literal in sorted(literals):
            checked += 1
            if literal not in observed:
                missing.append(f"{column}='{literal}' never occurs (observed: {sorted(observed)})")

    if not checked:
        return CheckResult(
            "filter_literals_match_data", SKIPPED, "no watched filter literals found"
        )
    if missing:
        return CheckResult(
            "filter_literals_match_data",
            FAILED,
            "; ".join(missing) + " -- such a filter returns zero rather than failing",
            {"literals_checked": checked, "missing": missing},
        )
    return CheckResult(
        "filter_literals_match_data",
        PASSED,
        f"all {checked} filter literal(s) occur in the data",
        {"literals_checked": checked},
    )


def check_status_code_domain(ch: Any) -> CheckResult:
    """An unknown status value means the exporter's vocabulary moved."""
    observed = {
        str(row[0]) for row in ch.query("SELECT DISTINCT StatusCode FROM otel_traces").result_rows
    }
    if not observed:
        return CheckResult("status_code_domain", SKIPPED, "otel_traces is empty")
    unknown = observed - KNOWN_STATUS_CODES
    if unknown:
        return CheckResult(
            "status_code_domain",
            FAILED,
            f"unexpected StatusCode value(s) {sorted(unknown)}; queries filtering the old "
            "vocabulary now match nothing",
            {"observed": sorted(observed)},
        )
    return CheckResult(
        "status_code_domain", PASSED, f"observed {sorted(observed)}", {"observed": sorted(observed)}
    )


def check_version_cardinality(ch: Any, *, lookback_minutes: int) -> CheckResult:
    """A differential needs two versions. One means nothing to compare."""
    count = _scalar(
        ch,
        "SELECT uniqExact(ResourceAttributes['service.version']) FROM otel_traces "
        "WHERE Timestamp >= now() - INTERVAL {lookback:UInt32} MINUTE",
        lookback=lookback_minutes,
    )
    count = int(count or 0)
    if count == 0:
        return CheckResult("version_cardinality", SKIPPED, "no telemetry in the window")
    if count < 2:
        return CheckResult(
            "version_cardinality",
            FAILED,
            f"only {count} deployed version in the last {lookback_minutes} minutes; "
            "a before/after comparison is not possible",
            {"versions": count},
        )
    return CheckResult(
        "version_cardinality", PASSED, f"{count} versions present", {"versions": count}
    )


def check_server_span_completeness(ch: Any, *, lookback_minutes: int) -> CheckResult:
    """Request capture reconstructs URLs from spans, so a gap there is silent."""
    rows = ch.query(
        "SELECT count(), countIf(SpanAttributes['http.url'] != '') FROM otel_traces "
        "WHERE SpanKind = 'Server' AND Timestamp >= now() - INTERVAL {lookback:UInt32} MINUTE",
        parameters={"lookback": lookback_minutes},
    ).result_rows
    total, populated = (int(rows[0][0]), int(rows[0][1])) if rows else (0, 0)
    if total == 0:
        return CheckResult("server_span_completeness", SKIPPED, "no server spans in the window")
    ratio = populated / total
    measured = {"total": total, "populated": populated, "ratio": round(ratio, 4)}
    if ratio < MIN_URL_COMPLETENESS:
        return CheckResult(
            "server_span_completeness",
            FAILED,
            f"only {ratio:.1%} of server spans carry http.url; captured requests would "
            "replay the wrong target",
            measured,
        )
    return CheckResult(
        "server_span_completeness", PASSED, f"{ratio:.1%} of server spans carry http.url", measured
    )


def check_referential_integrity(session: Any) -> CheckResult:
    """Orphans in the audit trail, including a reference the schema cannot enforce.

    `hypotheses.supporting_fact_ids` is a uuid array, so Postgres enforces only
    that it is non-empty -- not that the ids resolve. A hypothesis citing a fact
    that no longer exists still satisfies every constraint while citing nothing.
    """
    orphans: list[str] = []
    for label, sql in (
        (
            "facts without an incident",
            "SELECT count(*) FROM facts f LEFT JOIN incidents i ON f.incident_id = i.id "
            "WHERE i.id IS NULL",
        ),
        (
            "hypotheses without an incident",
            "SELECT count(*) FROM hypotheses h LEFT JOIN incidents i ON h.incident_id = i.id "
            "WHERE i.id IS NULL",
        ),
        (
            "verifications without a hypothesis",
            "SELECT count(*) FROM verifications v LEFT JOIN hypotheses h ON v.hypothesis_id = h.id "
            "WHERE h.id IS NULL",
        ),
        (
            "hypotheses citing facts that do not exist",
            "SELECT count(*) FROM hypotheses h WHERE EXISTS ("
            "  SELECT 1 FROM unnest(h.supporting_fact_ids) AS fid"
            "  WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.id = fid))",
        ),
    ):
        count = int(session.execute(text(sql)).scalar() or 0)
        if count:
            orphans.append(f"{count} {label}")

    if orphans:
        return CheckResult(
            "referential_integrity", FAILED, "; ".join(orphans), {"orphans": orphans}
        )
    return CheckResult("referential_integrity", PASSED, "no orphaned rows in the audit trail")


def run_all(
    *,
    ch: Any | None,
    session: Any | None,
    queries_dir: Path = QUERIES_DIR,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    max_staleness_minutes: int = DEFAULT_MAX_STALENESS_MINUTES,
) -> DataQualityReport:
    """Run every check, skipping cleanly when a store is unreachable."""
    results: list[CheckResult] = []

    if ch is None:
        results.append(CheckResult("warehouse_checks", SKIPPED, "ClickHouse is unreachable"))
    else:
        results.append(check_freshness(ch, max_staleness_minutes=max_staleness_minutes))
        results.append(check_filter_literals_match_data(ch, queries_dir=queries_dir))
        results.append(check_status_code_domain(ch))
        results.append(check_version_cardinality(ch, lookback_minutes=lookback_minutes))
        results.append(check_server_span_completeness(ch, lookback_minutes=lookback_minutes))

    if session is None:
        results.append(CheckResult("audit_trail_checks", SKIPPED, "PostgreSQL is unreachable"))
    else:
        results.append(check_referential_integrity(session))

    return DataQualityReport(results=tuple(results))
