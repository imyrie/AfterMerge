"""End-to-end: does AfterMerge reach the known answer?

This is the test the whole known-answer fixture exists for. The scenario file
records what actually happened; this asserts the pipeline rediscovers it from
telemetry rather than merely producing something plausible.

Requires a populated stack:  make up && make truncate && make dance
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine

from aftermerge.detector import service as detector_service
from aftermerge.detector.rules import SLO
from aftermerge.investigator import service as investigator_service
from aftermerge.report.render import render
from aftermerge.store import db
from aftermerge.telemetry import client

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = yaml.safe_load((ROOT / "scenarios" / "n_plus_one.yaml").read_text())


def _stack_ready() -> bool:
    try:
        engine = create_engine(db._dsn("postgres"), connect_args={"connect_timeout": 2})
        with engine.connect():
            pass
        engine.dispose()
        # Both versions must have recent route traffic. A bare count includes
        # spans hours old, which the detector's lookback window excludes -- the
        # suite would then fail for an environmental reason rather than a bug.
        commits = SCENARIO["commits"]
        rows = (
            client.get_client()
            .query(
                """
            SELECT uniqExact(ResourceAttributes['service.version'])
            FROM otel_traces
            WHERE ServiceName = {svc:String} AND SpanKind = 'Server'
              AND SpanName = {route:String}
              AND ResourceAttributes['service.version'] IN ({good:String}, {bad:String})
              AND Timestamp >= now() - INTERVAL 200 MINUTE
            """,
                parameters={
                    "svc": SCENARIO["route_service"],
                    "route": SCENARIO["route"],
                    "good": commits["good_sha"],
                    "bad": commits["bad_sha"],
                },
            )
            .result_rows
        )
        return bool(rows) and rows[0][0] == 2
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _stack_ready(),
    reason="no recent telemetry for both scenario commits; run `make truncate && make dance`",
)


@pytest.fixture(scope="module")
def investigation():
    """Run detection and investigation against a scratch database."""
    db.ensure_database("aftermerge_e2e")
    engine = db.get_engine("aftermerge_e2e")
    from aftermerge.store.repositories import DeploymentRepository
    from aftermerge.store.tables import Base

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    commits = SCENARIO["commits"]
    with db.session_scope(engine) as session:
        DeploymentRepository(session).record(
            service=SCENARIO["service"],
            commit_sha=commits["bad_sha"],
            prev_commit_sha=commits["good_sha"],
        )

    with db.session_scope(engine) as session:
        outcome = detector_service.detect(
            session,
            service=SCENARIO["service"],
            route_service=SCENARIO["route_service"],
            slo=SLO(route=SCENARIO["route"], p95_ms=float(SCENARIO["slo"]["p95_ms"])),
            lookback_minutes=240,
        )
        if outcome.incident is None:
            pytest.fail(f"detector found nothing: {outcome.detection.reasons}")
        result = investigator_service.investigate(session, outcome.incident, repo_root=ROOT)
        yield outcome, result, render(result)

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_the_regression_is_detected(investigation) -> None:
    outcome, _, _ = investigation
    assert outcome.detection.triggered is SCENARIO["expect"]["detected"]


def test_work_amplification_matches_the_recorded_measurement(investigation) -> None:
    outcome, _, _ = investigation
    amp = outcome.detection.amplification
    expected = SCENARIO["expect"]["db_spans_per_request"]

    assert amp is not None
    assert amp.baseline_per_request == pytest.approx(expected["good"], rel=0.1)
    assert amp.candidate_per_request == pytest.approx(expected["bad"], rel=0.1)


def test_the_responsible_file_is_identified(investigation) -> None:
    """The point of the whole pipeline: name the file without being told."""
    _, result, _ = investigation
    assert result.correlation is not None
    assert SCENARIO["expect"]["code_site"] in result.correlation.implicated_files


def test_the_expected_facts_were_recorded(investigation) -> None:
    _, result, _ = investigation
    kinds = {f.kind for f in result.facts}
    assert {"db_spans_per_request", "route_latency_quantiles"} <= kinds


def test_a_hypothesis_cites_evidence(investigation) -> None:
    _, result, _ = investigation
    assert result.hypotheses
    assert all(h.supporting_fact_ids for h in result.hypotheses)


def test_nothing_is_claimed_as_verified(investigation) -> None:
    """Slice 1 ends at inference. Level 3 must remain honestly empty."""
    _, _, markdown = investigation
    assert "**None.** Nothing above has been reproduced or verified" in markdown
