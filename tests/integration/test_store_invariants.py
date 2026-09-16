"""The trust ladder must be enforced, not merely documented.

These are the tests that make the project's central claim checkable: that a
conclusion cannot be recorded without evidence appropriate to its level.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from aftermerge.store import db
from aftermerge.store.repositories import (
    FactRepository,
    HypothesisRepository,
    IncidentRepository,
    VerificationRepository,
)
from aftermerge.store.tables import Base, Hypothesis
from aftermerge.telemetry.client import FactResult

TEST_DATABASE = "aftermerge_test"


def _postgres_available() -> bool:
    try:
        engine = create_engine(db._dsn("postgres"), connect_args={"connect_timeout": 2})
        with engine.connect():
            return True
    except Exception:
        return False
    finally:
        engine.dispose()


pytestmark = pytest.mark.skipif(not _postgres_available(), reason="postgres not running")


@pytest.fixture
def engine():
    db.ensure_database(TEST_DATABASE)
    eng = db.get_engine(TEST_DATABASE)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def incident_id(engine) -> uuid.UUID:
    with db.session_scope(engine) as session:
        incident = IncidentRepository(session).open(
            service="orders",
            route="GET /orders",
            baseline_version="good123",
            candidate_version="bad456",
            severity="major",
            summary="test incident",
            onset_at=datetime.now(UTC),
        )
        return incident.id


SAMPLE_RESULT = FactResult(
    query_name="span_count_per_trace",
    params={"service": "orders", "lookback_minutes": 60},
    columns=["version", "spans_per_request"],
    rows=[("good123", 2.0), ("bad456", 51.0)],
)


# --- level 1 -----------------------------------------------------------------


def test_fact_records_the_statement_that_produced_it(engine, incident_id) -> None:
    """A fact without its query is not reproducible, so both are stored."""
    with db.session_scope(engine) as session:
        FactRepository(session).record(
            incident_id=incident_id,
            kind="db_spans_per_request",
            result=SAMPLE_RESULT,
            value=51.0,
            unit="spans",
        )

    with db.session_scope(engine) as session:
        (fact,) = FactRepository(session).for_incident(incident_id)

    assert fact.query_name == "span_count_per_trace"
    assert fact.query_params["service"] == "orders"
    assert fact.raw_result["rows"][1] == ["bad456", 51.0]
    assert float(fact.value) == 51.0


# --- level 2 -----------------------------------------------------------------


def test_hypothesis_without_supporting_facts_is_rejected(engine, incident_id) -> None:
    with (
        pytest.raises(ValueError, match="at least one supporting fact"),
        db.session_scope(engine) as session,
    ):
        HypothesisRepository(session).propose(
            incident_id=incident_id,
            statement="PR #42 did it",
            kind="change_correlation",
            score=0.9,
            supporting_fact_ids=[],
            generated_by="test",
        )


def test_database_refuses_an_uncited_hypothesis_even_bypassing_the_repository(
    engine, incident_id
) -> None:
    """Defence in depth: the constraint lives in the schema, not only in Python."""
    with pytest.raises(IntegrityError), db.session_scope(engine) as session:
        session.add(
            Hypothesis(
                id=uuid.uuid4(),
                incident_id=incident_id,
                statement="unsupported claim",
                kind="guess",
                score=0.5,
                supporting_fact_ids=[],
                generated_by="bypass",
                created_at=datetime.now(UTC),
            )
        )


def test_score_must_be_a_probability(engine, incident_id) -> None:
    with pytest.raises(ValueError, match=r"within \[0, 1\]"), db.session_scope(engine) as session:
        HypothesisRepository(session).propose(
            incident_id=incident_id,
            statement="overconfident",
            kind="change_correlation",
            score=1.5,
            supporting_fact_ids=[uuid.uuid4()],
            generated_by="test",
        )


# --- level 3 -----------------------------------------------------------------


def _hypothesis(engine, incident_id) -> uuid.UUID:
    with db.session_scope(engine) as session:
        h = HypothesisRepository(session).propose(
            incident_id=incident_id,
            statement="the loop causes it",
            kind="change_correlation",
            score=0.8,
            supporting_fact_ids=[uuid.uuid4()],
            generated_by="test",
        )
        return h.id


def test_a_passing_process_confirms(engine, incident_id) -> None:
    hypothesis_id = _hypothesis(engine, incident_id)
    process = subprocess.run(["true"], capture_output=True, text=True)

    with db.session_scope(engine) as session:
        verification = VerificationRepository(session).record(
            hypothesis_id=hypothesis_id, method="differential_replay", process=process
        )
        assert verification.verdict == "confirmed"
        assert verification.exit_code == 0


def test_a_failing_process_cannot_be_recorded_as_confirmed(engine, incident_id) -> None:
    """The verdict is derived from the exit code, never supplied by the caller.

    This is what makes 'verified' unfakeable: there is no argument to override.
    """
    hypothesis_id = _hypothesis(engine, incident_id)
    process = subprocess.run(["false"], capture_output=True, text=True)

    with db.session_scope(engine) as session:
        verification = VerificationRepository(session).record(
            hypothesis_id=hypothesis_id, method="differential_replay", process=process
        )

    assert verification.verdict == "refuted"
    assert verification.exit_code == 1


def test_verification_requires_a_real_process_object(engine, incident_id) -> None:
    """A model can produce a convincing string; it cannot produce this object."""
    hypothesis_id = _hypothesis(engine, incident_id)

    with pytest.raises(AttributeError), db.session_scope(engine) as session:
        VerificationRepository(session).record(
            hypothesis_id=hypothesis_id,
            method="wishful_thinking",
            process="the tests passed, trust me",  # type: ignore[arg-type]
        )


def test_stdout_is_captured_for_audit(engine, incident_id) -> None:
    hypothesis_id = _hypothesis(engine, incident_id)
    process = subprocess.run(["echo", "51 queries -> 2"], capture_output=True, text=True)

    with db.session_scope(engine) as session:
        verification = VerificationRepository(session).record(
            hypothesis_id=hypothesis_id, method="replay", process=process
        )

    assert "51 queries -> 2" in (verification.stdout_excerpt or "")


# --- incidents ---------------------------------------------------------------


def test_invalid_severity_is_rejected(engine) -> None:
    with (
        pytest.raises(ValueError, match="severity must be one of"),
        db.session_scope(engine) as session,
    ):
        IncidentRepository(session).open(
            service="orders",
            route="GET /orders",
            baseline_version="a",
            candidate_version="b",
            severity="catastrophic",
            summary="",
            onset_at=datetime.now(UTC),
        )
