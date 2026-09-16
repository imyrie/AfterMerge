"""AfterMerge's durable audit trail.

Deliberately a **separate database** from the demo app's. shopdemo is the system
under test: later slices wipe and reseed it, and the reproducer spins up throwaway
copies. AfterMerge's own record of what it observed must outlive all of that.

The schema encodes the project's three trust levels as three tables:

    facts          level 1  observed      written only by the query runner
    hypotheses     level 2  inferred      must cite facts; ranked, never asserted
    verifications  level 3  verified      requires a real process exit code

The separation is the point. It is what stops the project from degrading into
"a language model looked at some telemetry and guessed".
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Deployment(Base):
    """One observed deploy of one service.

    Spans already carry `service.version`, which is enough to *split* telemetry by
    version. This table adds what telemetry cannot know: which commit preceded
    which, when the changeover happened in wall-clock terms, and which pull
    request it came from -- the join keys change correlation needs.

    Repeat rows for the same (service, commit_sha) are allowed on purpose: a
    rollback is a real deploy of an already-seen commit, and collapsing those
    would erase the event that matters most.
    """

    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    service: Mapped[str] = mapped_column(String(128))
    commit_sha: Mapped[str] = mapped_column(String(64))
    prev_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repo: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Change correlation always asks "what deployed to this service just
        # before time T", so the index matches that access pattern.
        Index("ix_deployments_service_time", "service", "deployed_at"),
    )

    def __repr__(self) -> str:
        return f"<Deployment {self.service}@{self.commit_sha} at {self.deployed_at.isoformat()}>"


class CapturedRequest(Base):
    """A production request, reconstructed from telemetry and safe to replay.

    Captured from spans rather than from application middleware. That keeps
    AfterMerge read-only -- no redeploy, no request-path code, and it works
    retroactively on traffic that has already happened.

    The trade-off is fidelity: spans carry no request body, so a mutating
    request can be recorded but not faithfully reproduced. Those are stored with
    `replay_safe = False` and a stated reason rather than silently dropped,
    because "we saw this and cannot replay it" is useful information.
    """

    __tablename__ = "captured_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("incidents.id"), nullable=True
    )

    method: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(2048))
    query: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    replay_safe: Mapped[bool] = mapped_column(Boolean, default=False)
    unreplayable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observations: Mapped[int] = mapped_column(Integer, default=1)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_duration_ms: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_captured_requests_incident", "incident_id"),)

    def __repr__(self) -> str:
        return f"<CapturedRequest {self.method} {self.path} safe={self.replay_safe}>"


class Incident(Base):
    """A detected regression. Created only by the deterministic detector."""

    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    service: Mapped[str] = mapped_column(String(128))
    route: Mapped[str] = mapped_column(String(256))

    # Which two versions were compared. Recorded so the incident can be
    # re-derived later without guessing what "before" meant at the time.
    baseline_version: Mapped[str] = mapped_column(String(64))
    candidate_version: Mapped[str] = mapped_column(String(64))
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("deployments.id"), nullable=True
    )

    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="open")
    summary: Mapped[str] = mapped_column(Text)

    onset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    facts: Mapped[list[Fact]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    hypotheses: Mapped[list[Hypothesis]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("severity IN ('minor','major','critical')", name="ck_incidents_severity"),
        Index("ix_incidents_service_time", "service", "detected_at"),
    )

    def __repr__(self) -> str:
        return f"<Incident {self.service} {self.route} {self.severity}>"


class Fact(Base):
    """LEVEL 1 -- an observed measurement.

    Stores the query name, its parameters, and the raw result alongside the
    scalar value. That is what makes a fact reproducible: anyone can re-run the
    named statement with the recorded parameters and get the same number.

    Never written by a language model. `FactRepository.record` is reachable only
    from the query runner.
    """

    __tablename__ = "facts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("incidents.id"))

    kind: Mapped[str] = mapped_column(String(64))
    query_name: Mapped[str] = mapped_column(String(128))
    query_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    raw_result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    value: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="facts")

    __table_args__ = (Index("ix_facts_incident_kind", "incident_id", "kind"),)

    def __repr__(self) -> str:
        return f"<Fact {self.kind}={self.value}{self.unit or ''}>"


class Hypothesis(Base):
    """LEVEL 2 -- an inference, ranked and never asserted.

    `supporting_fact_ids` is constrained to be non-empty at the database level.
    An inference that cites no observation is not a hypothesis, it is a guess,
    and the schema refuses to store one.
    """

    __tablename__ = "hypotheses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("incidents.id"))

    statement: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(64))
    # Computed by the scorer, never supplied by a model.
    score: Mapped[float] = mapped_column(Numeric)
    supporting_fact_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid))
    generated_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="hypotheses")
    verifications: Mapped[list[Verification]] = relationship(
        back_populates="hypothesis", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("cardinality(supporting_fact_ids) > 0", name="ck_hypotheses_cites_facts"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_hypotheses_score_range"),
    )

    def __repr__(self) -> str:
        return f"<Hypothesis {self.kind} score={self.score}>"


class Verification(Base):
    """LEVEL 3 -- a conclusion backed by something that actually ran.

    `exit_code` is NOT NULL, and the repository accepts only a
    `subprocess.CompletedProcess`. There is deliberately no code path that lets a
    language model claim something was verified: it cannot produce that object.

    Three verdicts, not two. A run that crashed or could not start is `errored`,
    never `refuted`: recording a broken harness as evidence against a hypothesis
    would be worse than recording nothing.
    """

    __tablename__ = "verifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("hypotheses.id"))

    method: Mapped[str] = mapped_column(String(64))
    command: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int] = mapped_column(Integer)
    stdout_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    verdict: Mapped[str] = mapped_column(String(16))
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    hypothesis: Mapped[Hypothesis] = relationship(back_populates="verifications")

    __table_args__ = (
        CheckConstraint(
            "verdict IN ('confirmed','refuted','errored')", name="ck_verifications_verdict"
        ),
    )

    def __repr__(self) -> str:
        return f"<Verification {self.method} {self.verdict} exit={self.exit_code}>"
