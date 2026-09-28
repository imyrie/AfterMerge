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

#: Values the ClickHouse exporter actually writes for span status. An observed
#: value outside this set means the exporter changed and every query filtering
#: on the old vocabulary has silently started matching nothing.
KNOWN_STATUS_CODES = frozenset({"Unset", "Ok", "Error"})

#: Columns whose filter literals are worth verifying against the data. These are
#: low-cardinality enums where a typo or a version change produces an empty
#: match rather than an error.
WATCHED_FILTER_COLUMNS = ("StatusCode", "SpanKind")

_FILTER_LITERAL = r"{column}\s*(?:=|IN\s*\()\s*'([^']+)'"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    measured: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == FAILED


@dataclass(frozen=True)
class DataQualityReport:
    results: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        """Skips do not fail the run, but a run of only skips proves nothing."""
        return any(r.status == PASSED for r in self.results) and not any(
            r.failed for r in self.results
        )

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.failed)

    @property
    def summary(self) -> str:
        counts = {
            s: sum(1 for r in self.results if r.status == s) for s in (PASSED, FAILED, SKIPPED)
        }
        if self.passed:
            return f"{counts[PASSED]} checks passed, {counts[SKIPPED]} skipped"
        if not self.results:
            return "no checks ran"
        if counts[PASSED] == 0:
            return f"no check produced evidence ({counts[SKIPPED]} skipped)"
        return f"{counts[FAILED]} check(s) failed: " + "; ".join(
            f"{r.name} -- {r.detail}" for r in self.failures
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "checks": [
                {"name": r.name, "status": r.status, "detail": r.detail, "measured": r.measured}
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
