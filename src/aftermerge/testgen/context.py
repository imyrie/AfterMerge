"""Assembling what a regression test needs to know.

Deterministic, and deliberately separate from generation. Whatever writes the
test -- a template or a language model -- works from this same structured
evidence, so the two paths cannot diverge on the facts and a generated test can
always be traced back to the measurements that justify it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

#: Query parameters that plausibly control how many rows a response returns.
#: An N+1 is a *scaling* fault, so when one of these is present the generated
#: test can assert invariance under it rather than guessing a magic threshold.
SIZE_PARAMETERS = ("limit", "page_size", "per_page", "count", "size", "top")


@dataclass(frozen=True)
class TestContext:
    service: str
    route: str
    baseline_version: str
    candidate_version: str

    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)

    baseline_spans_per_request: float = 0.0
    candidate_spans_per_request: float = 0.0
    code_site: str | None = None
    changed_files: tuple[str, ...] = ()

    @property
    def amplification(self) -> float:
        if not self.baseline_spans_per_request:
            return math.inf if self.candidate_spans_per_request else 1.0
        return self.candidate_spans_per_request / self.baseline_spans_per_request

    @property
    def size_parameter(self) -> tuple[str, int] | None:
        """A query parameter that looks like a page size, with its value."""
        for name, value in self.query.items():
            if name.lower() in SIZE_PARAMETERS and str(value).isdigit():
                return name, int(value)
        return None

    @property
    def threshold(self) -> int:
        """The most queries per request the good build should ever need.

        Derived from what the baseline actually did, with headroom for
        legitimate variation, and it must sit far below the candidate or the
        test cannot discriminate between the two commits.
        """
        floor = math.ceil(self.baseline_spans_per_request) + 1
        return max(floor, math.ceil(self.baseline_spans_per_request * 2))

    @property
    def discriminates(self) -> bool:
        """Could a test built from this evidence separate the two builds?

        A test that both commits pass, or both fail, is worthless. Checking here
        rejects a hopeless candidate before anything is written to disk -- the
        gate would catch it anyway, but not before spending two sandbox builds.

        A scaling assertion needs the work to genuinely have grown; a threshold
        assertion needs a value the baseline clears and the candidate does not.
        """
        if self.size_parameter is not None:
            return self.amplification > 1.5
        return self.baseline_spans_per_request <= self.threshold < self.candidate_spans_per_request

    @property
    def test_name(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", self.route.lower()).strip("_") or "route"
        return f"test_{slug}_does_not_scale_database_work"

    @property
    def module_name(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", f"{self.service}_{self.route}".lower()).strip("_")
        return f"test_{slug}_regression"


def _totals_by_version(
    raw_result: dict[str, Any], baseline: str, candidate: str
) -> tuple[float, float, str | None]:
    columns: list[str] = raw_result.get("columns", [])
    rows: list[list[Any]] = raw_result.get("rows", [])
    if not {"version", "code_site", "spans_per_request"} <= set(columns):
        return 0.0, 0.0, None

    v_idx = columns.index("version")
    s_idx = columns.index("code_site")
    p_idx = columns.index("spans_per_request")

    base = cand = 0.0
    top_site: str | None = None
    top_value = 0.0
    for row in rows:
        value = float(row[p_idx])
        if row[v_idx] == baseline:
            base += value
        elif row[v_idx] == candidate:
            cand += value
            if value > top_value:
                top_value, top_site = value, str(row[s_idx])
    return base, cand, top_site


def build(
    incident: Any, facts: list[Any], captured: Any, correlation: Any | None = None
) -> TestContext:
    """Gather the evidence a regression test has to encode."""
    span_fact = next((f for f in facts if f.kind == "db_spans_per_request"), None)
    baseline = candidate = 0.0
    code_site: str | None = None
    if span_fact is not None:
        baseline, candidate, code_site = _totals_by_version(
            span_fact.raw_result, incident.baseline_version, incident.candidate_version
        )

    changed = tuple(
        c.source_path for c in getattr(correlation, "changed_files", ()) if c.source_path
    )

    return TestContext(
        service=incident.service,
        route=incident.route,
        baseline_version=incident.baseline_version,
        candidate_version=incident.candidate_version,
        method=captured.method,
        path=captured.path,
        query=dict(captured.query),
        baseline_spans_per_request=baseline,
        candidate_spans_per_request=candidate,
        code_site=code_site,
        changed_files=changed,
    )
