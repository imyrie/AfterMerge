"""Narrow write APIs over the audit trail.

Repositories exist to keep writes intentional. In later slices this is what
enforces the trust levels -- notably that a `verifications` row can only be
created from a real `CompletedProcess`, so there is no code path by which a
language model can assert that something was verified.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftermerge.store.tables import Deployment


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

    def list_recent(self, service: str | None = None, limit: int = 20) -> list[Deployment]:
        stmt = select(Deployment).order_by(Deployment.deployed_at.desc()).limit(limit)
        if service is not None:
            stmt = stmt.where(Deployment.service == service)
        return list(self._session.execute(stmt).scalars().all())
