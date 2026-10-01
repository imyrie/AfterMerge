"""Data quality checks that gate analysis.

Motivated by a defect that survived three slices unnoticed: `latency_quantiles.sql`
filtered on `StatusCode = 'STATUS_CODE_ERROR'` while ClickHouse writes the short
form `'Error'`, so the error column read zero for every measurement ever taken.
Nothing failed. The number was simply wrong, and looked plausible.

That is the shape of defect these checks exist for. A pipeline that only notices
errors when something crashes will report confident, wrong numbers indefinitely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

#: A failed check either stops the pipeline or annotates it. The distinction is
#: whether the finding means the *data* is unfit to analyse (blocking) or means
#: one metric needs a caveat while the rest of the data is sound (advisory).
#: Blocking is the default, so a new check has to opt out deliberately.
BLOCKING = "blocking"
ADVISORY = "advisory"

#: Values the ClickHouse exporter actually writes for span status. An observed
#: value outside this set means the exporter changed and every query filtering
#: on the old vocabulary has silently started matching nothing.
KNOWN_STATUS_CODES = frozenset({"Unset", "Ok", "Error"})

#: Likewise for span kind.
KNOWN_SPAN_KINDS = frozenset(
    {"Unspecified", "Internal", "Server", "Client", "Producer", "Consumer"}
)

#: Columns whose filter literals are worth verifying against the data, each with
#: the vocabulary the exporter can emit. These are low-cardinality enums where a
#: typo or a version change produces an empty match rather than an error.
WATCHED_FILTER_COLUMNS = ("StatusCode", "SpanKind")

COLUMN_VOCABULARIES = {
    "StatusCode": KNOWN_STATUS_CODES,
    "SpanKind": KNOWN_SPAN_KINDS,
}

_FILTER_LITERAL = r"{column}\s*(?:=|IN\s*\()\s*'([^']+)'"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    measured: dict[str, Any] = field(default_factory=dict)
    severity: str = BLOCKING

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    @property
    def blocks(self) -> bool:
        return self.failed and self.severity == BLOCKING


@dataclass(frozen=True)
class DataQualityReport:
    results: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        """Skips do not fail the run, but a run of only skips proves nothing.

        Advisory failures do not fail the run either -- they are caveats on one
        metric, not grounds to refuse the data. They still appear in `summary`.
        """
        return any(r.status == PASSED for r in self.results) and not self.blocking_failures

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.failed)

    @property
    def blocking_failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.blocks)

    @property
    def advisories(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.failed and r.severity == ADVISORY)

    @property
    def summary(self) -> str:
        counts = {
            s: sum(1 for r in self.results if r.status == s) for s in (PASSED, FAILED, SKIPPED)
        }
        advisory_note = ""
        if self.advisories:
            advisory_note = f", {len(self.advisories)} advisory: " + "; ".join(
                f"{r.name} -- {r.detail}" for r in self.advisories
            )
        if self.passed:
            return f"{counts[PASSED]} checks passed, {counts[SKIPPED]} skipped{advisory_note}"
        if not self.results:
            return "no checks ran"
        if counts[PASSED] == 0:
            return f"no check produced evidence ({counts[SKIPPED]} skipped)"
        return (
            f"{len(self.blocking_failures)} check(s) failed: "
            + "; ".join(f"{r.name} -- {r.detail}" for r in self.blocking_failures)
            + advisory_note
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "checks": [
                {
                    "name": r.name,
                    "status": r.status,
                    "severity": r.severity,
                    "detail": r.detail,
                    "measured": r.measured,
                }
                for r in self.results
            ],
        }


def filter_literals(sql_dir: Path, column: str) -> set[str]:
    """Literals the catalog compares `column` against.

    Deliberately a regex over the .sql files rather than a parser: the catalog is
    small, hand-written and stable, and the failure mode being guarded against is
    a typo in a literal, which a regex sees perfectly well.
    """
    pattern = re.compile(_FILTER_LITERAL.format(column=re.escape(column)), re.IGNORECASE)
    found: set[str] = set()
    for path in sorted(sql_dir.glob("*.sql")):
        found.update(pattern.findall(path.read_text()))
    return found
