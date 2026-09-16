"""Correlation scoring, including the cases where it should admit weakness."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from aftermerge.investigator.correlation import TEMPORAL_ONLY_CEILING, correlate
from aftermerge.store.tables import Fact, Incident

REPO = Path(__file__).resolve().parents[2]


def incident() -> Incident:
    return Incident(
        id=uuid.uuid4(),
        service="orders",
        route="GET /orders",
        baseline_version="cbb4790",
        candidate_version="8b4fd77",
        severity="critical",
        status="open",
        summary="test",
        onset_at=datetime.now(UTC),
        detected_at=datetime.now(UTC),
    )


def span_fact(rows: list[list[object]]) -> Fact:
    return Fact(
        id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        kind="db_spans_per_request",
        query_name="span_count_per_trace",
        query_params={},
        raw_result={"columns": ["version", "code_site", "spans_per_request"], "rows": rows},
        observed_at=datetime.now(UTC),
    )


def test_new_work_in_a_changed_file_scores_fully() -> None:
    fact = span_fact(
        [["cbb4790", "orders/repository.py", 2.0], ["8b4fd77", "orders/repository.py", 51.0]]
    )
    result = correlate(incident(), [fact], repo_root=REPO, source_prefix="fixtures/shopdemo")

    assert result is not None
    assert result.basis == "code_site_overlap"
    assert result.score == 1.0
    assert result.implicated_files == ("orders/repository.py",)
    assert "49 of the 49" in result.statement


def test_new_work_outside_the_diff_scores_zero_and_says_so() -> None:
    """The diff genuinely fails to explain the regression -- do not pretend otherwise."""
    fact = span_fact(
        [["cbb4790", "orders/elsewhere.py", 2.0], ["8b4fd77", "orders/elsewhere.py", 51.0]]
    )
    result = correlate(incident(), [fact], repo_root=REPO, source_prefix="fixtures/shopdemo")

    assert result is not None
    assert result.score == 0.0
    assert "none of the" in result.statement


def test_partial_attribution_scores_proportionally() -> None:
    fact = span_fact(
        [
            ["cbb4790", "orders/repository.py", 2.0],
            ["8b4fd77", "orders/repository.py", 32.0],  # +30, in the diff
            ["cbb4790", "orders/elsewhere.py", 1.0],
            ["8b4fd77", "orders/elsewhere.py", 11.0],  # +10, not in the diff
        ]
    )
    result = correlate(incident(), [fact], repo_root=REPO, source_prefix="fixtures/shopdemo")

    assert result is not None
    assert result.score == 0.75  # 30 of 40


def test_no_work_change_falls_back_to_timing_and_is_capped() -> None:
    """Without a mechanical link the claim is weak, and the score must reflect that."""
    fact = span_fact(
        [["cbb4790", "orders/repository.py", 2.0], ["8b4fd77", "orders/repository.py", 2.0]]
    )
    result = correlate(incident(), [fact], repo_root=REPO, source_prefix="fixtures/shopdemo")

    assert result is not None
    assert result.basis == "temporal_only"
    assert result.score == TEMPORAL_ONLY_CEILING
    assert "rests on timing alone" in result.statement


def test_missing_span_evidence_still_produces_a_timing_correlation() -> None:
    result = correlate(incident(), [], repo_root=REPO, source_prefix="fixtures/shopdemo")
    assert result is not None
    assert result.basis == "temporal_only"
