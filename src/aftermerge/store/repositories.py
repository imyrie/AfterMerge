"""Narrow write APIs over the audit trail.

Repositories exist to make the trust levels unfakeable rather than merely
documented. Each level's write path demands evidence appropriate to that level:

    FactRepository.record          requires a FactResult from an executed query
    HypothesisRepository.propose   requires at least one supporting fact id
    VerificationRepository.record  requires a subprocess.CompletedProcess

The last one is the load-bearing case. A language model can produce a persuasive
string, but it cannot produce a CompletedProcess -- so there is no code path by
which it can assert that something was verified.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftermerge.store.tables import (
    CapturedRequest,
    Deployment,
    Fact,
    Hypothesis,
    Incident,
    Verification,
)
from aftermerge.telemetry.client import FactResult

SEVERITIES = ("minor", "major", "critical")


class DeploymentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        service: str,
        commit_sha: str,
        prev_commit_sha: str | None = None,
        repo: str | None = None,
        pr_number: int | None = None,
        actor: str | None = None,
        deployed_at: datetime | None = None,
    ) -> Deployment:
        deployment = Deployment(
            id=uuid.uuid4(),
            service=service,
            commit_sha=commit_sha,
            prev_commit_sha=prev_commit_sha,
            repo=repo,
            pr_number=pr_number,
            actor=actor,
            deployed_at=deployed_at or datetime.now(UTC),
        )
        self._session.add(deployment)
        self._session.flush()
        return deployment

    def latest_before(self, service: str, when: datetime) -> Deployment | None:
        """The deploy in effect at `when`.

        This is the query change correlation runs against an incident's onset
        time to find the candidate change.
        """
        stmt = (
            select(Deployment)
            .where(Deployment.service == service, Deployment.deployed_at <= when)
            .order_by(Deployment.deployed_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()

    def latest(self, service: str) -> Deployment | None:
        stmt = (
            select(Deployment)
            .where(Deployment.service == service)
            .order_by(Deployment.deployed_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()

    def list_recent(self, service: str | None = None, limit: int = 20) -> list[Deployment]:
        stmt = select(Deployment).order_by(Deployment.deployed_at.desc()).limit(limit)
        if service is not None:
            stmt = stmt.where(Deployment.service == service)
        return list(self._session.execute(stmt).scalars().all())


class IncidentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def open(
        self,
        *,
        service: str,
        route: str,
        baseline_version: str,
        candidate_version: str,
        severity: str,
        summary: str,
        onset_at: datetime,
        deployment_id: uuid.UUID | None = None,
    ) -> Incident:
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {severity!r}")

        incident = Incident(
            id=uuid.uuid4(),
            service=service,
            route=route,
            baseline_version=baseline_version,
            candidate_version=candidate_version,
            deployment_id=deployment_id,
            severity=severity,
            status="open",
            summary=summary,
            onset_at=onset_at,
            detected_at=datetime.now(UTC),
        )
        self._session.add(incident)
        self._session.flush()
        return incident

    def get(self, incident_id: uuid.UUID) -> Incident | None:
        return self._session.get(Incident, incident_id)

    def list_recent(self, limit: int = 20) -> list[Incident]:
        stmt = select(Incident).order_by(Incident.detected_at.desc()).limit(limit)
        return list(self._session.execute(stmt).scalars().all())


class FactRepository:
    """LEVEL 1. Facts may only be created from an executed query."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        incident_id: uuid.UUID,
        kind: str,
        result: FactResult,
        value: float | None = None,
        unit: str | None = None,
    ) -> Fact:
        """Persist an observation.

        Takes the `FactResult` returned by the query runner rather than loose
        numbers, so a fact cannot exist without the statement that produced it.
        """
        fact = Fact(
            id=uuid.uuid4(),
            incident_id=incident_id,
            kind=kind,
            query_name=result.query_name,
            query_params=dict(result.params),
            raw_result={"columns": result.columns, "rows": [list(r) for r in result.rows]},
            value=value,
            unit=unit,
            observed_at=datetime.now(UTC),
        )
        self._session.add(fact)
        self._session.flush()
        return fact

    def for_incident(self, incident_id: uuid.UUID) -> list[Fact]:
        stmt = select(Fact).where(Fact.incident_id == incident_id).order_by(Fact.observed_at)
        return list(self._session.execute(stmt).scalars().all())


class HypothesisRepository:
    """LEVEL 2. Inferences must cite observations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def propose(
        self,
        *,
        incident_id: uuid.UUID,
        statement: str,
        kind: str,
        score: float,
        supporting_fact_ids: Sequence[uuid.UUID],
        generated_by: str,
    ) -> Hypothesis:
        # Checked here as well as in the database so the failure names the rule
        # rather than surfacing as an opaque constraint violation.
        if not supporting_fact_ids:
            raise ValueError("a hypothesis must cite at least one supporting fact")
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"score must be within [0, 1], got {score}")

        hypothesis = Hypothesis(
            id=uuid.uuid4(),
            incident_id=incident_id,
            statement=statement,
            kind=kind,
            score=score,
            supporting_fact_ids=list(supporting_fact_ids),
            generated_by=generated_by,
            created_at=datetime.now(UTC),
        )
        self._session.add(hypothesis)
        self._session.flush()
        return hypothesis

    def supersede(
        self,
        *,
        incident_id: uuid.UUID,
        statement: str,
        kind: str,
        score: float,
        supporting_fact_ids: Sequence[uuid.UUID],
        generated_by: str,
    ) -> Hypothesis:
        """Propose, or refresh the existing conclusion from the same source.

        Re-running an investigation must not stack a second, contradictory
        hypothesis beside the first: the stale one can outrank the current one on
        score and be the only thing a reader sees.

        Updated in place rather than deleted and re-inserted, because
        verifications reference a hypothesis by id -- deleting one would cascade
        and destroy level-3 evidence that was expensive and honest to obtain.
        """
        existing = (
            self._session.execute(
                select(Hypothesis).where(
                    # Matched on source alone. The kind can legitimately change
                    # between runs -- a mechanical correlation weakens to a timing
                    # one when the work delta turns out to be noise -- and that is
                    # an update to one claim, not a second contradictory claim.
                    Hypothesis.incident_id == incident_id,
                    Hypothesis.generated_by == generated_by,
                )
            )
            .scalars()
            .first()
        )

        if existing is None:
            return self.propose(
                incident_id=incident_id,
                statement=statement,
                kind=kind,
                score=score,
                supporting_fact_ids=supporting_fact_ids,
                generated_by=generated_by,
            )

        if not supporting_fact_ids:
            raise ValueError("a hypothesis must cite at least one supporting fact")
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"score must be within [0, 1], got {score}")

        existing.statement = statement
        existing.kind = kind
        existing.score = score
        existing.supporting_fact_ids = list(supporting_fact_ids)
        existing.created_at = datetime.now(UTC)
        self._session.flush()
        return existing

    def for_incident(self, incident_id: uuid.UUID) -> list[Hypothesis]:
        stmt = (
            select(Hypothesis)
            .where(Hypothesis.incident_id == incident_id)
            .order_by(Hypothesis.score.desc())
        )
        return list(self._session.execute(stmt).scalars().all())


class VerificationRepository:
    """LEVEL 3. Conclusions require something that actually ran."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        hypothesis_id: uuid.UUID,
        method: str,
        process: subprocess.CompletedProcess[str],
        metrics: dict[str, Any] | None = None,
        stdout_limit: int = 4000,
        inconclusive_codes: frozenset[int] = frozenset(),
    ) -> Verification:
        """Persist the outcome of a real execution.

        The `process` argument is the enforcement mechanism. Verdict is derived
        from the exit code, not supplied by the caller, so nothing can record a
        confirmation for a command that failed.

        `inconclusive_codes` lets a caller declare which codes its command uses
        for "could not run" -- exit 2 for a missing prerequisite, say. Those
        become `errored` rather than `refuted`. The caller states its convention;
        it still cannot state the outcome.
        """
        command = (
            process.args
            if isinstance(process.args, str)
            else " ".join(str(a) for a in process.args)
        )
        verification = Verification(
            id=uuid.uuid4(),
            hypothesis_id=hypothesis_id,
            method=method,
            command=command,
            exit_code=process.returncode,
            stdout_excerpt=(process.stdout or "")[:stdout_limit] or None,
            metrics=metrics or {},
            verdict=(
                "confirmed"
                if process.returncode == 0
                else "errored"
                if process.returncode in inconclusive_codes
                else "refuted"
            ),
            ran_at=datetime.now(UTC),
        )
        self._session.add(verification)
        self._session.flush()
        return verification

    def for_hypothesis(self, hypothesis_id: uuid.UUID) -> list[Verification]:
        stmt = (
            select(Verification)
            .where(Verification.hypothesis_id == hypothesis_id)
            .order_by(Verification.ran_at)
        )
        return list(self._session.execute(stmt).scalars().all())


class CapturedRequestRepository:
    """Production requests reconstructed from telemetry."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        envelope: Any,
        incident_id: uuid.UUID | None = None,
        observations: int = 1,
        status_code: int | None = None,
        max_duration_ms: float | None = None,
        unreplayable_reason: str | None = None,
    ) -> CapturedRequest:
        """Persist one captured request.

        `replay_safe` is computed here, never taken from the caller: it is true
        only when the envelope was sanitised *and* nothing made it unreplayable.
        """
        captured = CapturedRequest(
            id=uuid.uuid4(),
            incident_id=incident_id,
            method=envelope.method,
            path=envelope.path,
            query=dict(envelope.query),
            headers=dict(envelope.headers),
            replay_safe=bool(envelope.replay_safe and unreplayable_reason is None),
            unreplayable_reason=unreplayable_reason,
            source_trace_id=envelope.source_trace_id,
            observations=observations,
            status_code=status_code,
            max_duration_ms=max_duration_ms,
            captured_at=datetime.now(UTC),
        )
        self._session.add(captured)
        self._session.flush()
        return captured

    def for_incident(self, incident_id: uuid.UUID) -> list[CapturedRequest]:
        stmt = (
            select(CapturedRequest)
            .where(CapturedRequest.incident_id == incident_id)
            .order_by(CapturedRequest.max_duration_ms.desc().nullslast())
        )
        return list(self._session.execute(stmt).scalars().all())

    def replayable_for_incident(self, incident_id: uuid.UUID) -> list[CapturedRequest]:
        """Only requests that can actually be reproduced.

        Replay should never have to decide this for itself, and should never see
        a request it must not send.
        """
        stmt = (
            select(CapturedRequest)
            .where(
                CapturedRequest.incident_id == incident_id,
                CapturedRequest.replay_safe.is_(True),
            )
            .order_by(CapturedRequest.max_duration_ms.desc().nullslast())
        )
        return list(self._session.execute(stmt).scalars().all())
