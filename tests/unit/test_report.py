"""The report must always show all three trust levels, including empty ones."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from aftermerge.investigator.service import Investigation
from aftermerge.report.render import render
from aftermerge.store.tables import Fact, Hypothesis, Incident


def _investigation(
    *, with_hypothesis: bool = True, notes: list[str] | None = None
) -> Investigation:
    incident = Incident(
        id=uuid.uuid4(),
        service="orders",
        route="GET /orders",
        baseline_version="cbb4790",
        candidate_version="8b4fd77",
        severity="critical",
        status="open",
        summary="database work per request rose 25.5x; candidate is slower",
        onset_at=datetime.now(UTC),
        detected_at=datetime.now(UTC),
    )
    fact = Fact(
        id=uuid.uuid4(),
        incident_id=incident.id,
        kind="db_spans_per_request",
        query_name="span_count_per_trace",
        query_params={},
        raw_result={"columns": [], "rows": []},
        value=51.0,
        unit="spans",
        observed_at=datetime.now(UTC),
    )
    hypotheses = []
    if with_hypothesis:
        hypotheses.append(
            Hypothesis(
                id=uuid.uuid4(),
                incident_id=incident.id,
                statement="Commit 8b4fd77 modified orders/repository.py",
                kind="change_correlation",
                score=1.0,
                supporting_fact_ids=[fact.id],
                generated_by="test",
                created_at=datetime.now(UTC),
            )
        )
    return Investigation(
        incident=incident,
        facts=[fact],
        hypotheses=hypotheses,
        correlation=None,
        notes=notes or [],
    )


def test_all_three_levels_are_always_present() -> None:
    markdown = render(_investigation())
    for heading in ("## Observed facts", "## Hypotheses", "## Verified conclusions"):
        assert heading in markdown


def test_absence_of_verification_is_stated_not_omitted() -> None:
    """A reader must not mistake an empty section for a confirmed conclusion."""
    markdown = render(_investigation())
    assert "**None.** Nothing above has been reproduced or verified" in markdown


def test_score_is_labelled_as_work_explained_not_confidence() -> None:
    """The number is a measured fraction; calling it confidence overstates it."""
    markdown = render(_investigation())
    assert "explains 100% of the new work" in markdown
    assert "confidence" not in markdown


def test_facts_cite_the_query_that_produced_them() -> None:
    assert "`span_count_per_trace`" in render(_investigation())


def test_limitations_are_reported_when_present() -> None:
    markdown = render(_investigation(notes=["change correlation unavailable: bad object"]))
    assert "## Limitations" in markdown
    assert "bad object" in markdown


def test_no_hypotheses_renders_cleanly() -> None:
    assert "None proposed." in render(_investigation(with_hypothesis=False))


def test_a_timing_only_hypothesis_is_not_labelled_as_explaining_work() -> None:
    """Saying "explains 35% of the new work" when no new work exists invents a measurement."""
    investigation = _investigation()
    investigation.hypotheses[0].kind = "temporal_correlation"
    investigation.hypotheses[0].score = 0.35

    markdown = render(investigation)
    assert "timing correlation only (score 0.35)" in markdown
    assert "of the new work" not in markdown
