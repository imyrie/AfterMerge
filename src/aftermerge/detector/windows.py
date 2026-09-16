"""Choosing what to compare against what.

Windows are defined by **deployed version**, not by wall-clock time. Comparing
"the last 10 minutes against the 30 before that" breaks whenever deploys overlap,
a rollout is gradual, or a rollback happens -- all of which put two versions in
the same time window and silently average them together.

Splitting on `service.version` is correct in all of those cases, and the deploy
record supplies the ordering that telemetry alone cannot.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from aftermerge.store.repositories import DeploymentRepository


@dataclass(frozen=True)
class ComparisonWindow:
    service: str
    baseline_version: str
    candidate_version: str
    onset_at: datetime
    deployment_id: uuid.UUID | None


class NoComparisonAvailable(Exception):
    """Raised when there is nothing meaningful to compare.

    A distinct outcome from "no regression found": the detector was unable to
    look, which must never be reported as an all-clear.
    """


def from_latest_deployment(session: Session, service: str) -> ComparisonWindow:
    """Build the comparison implied by the most recent deploy of `service`."""
    deployment = DeploymentRepository(session).latest(service)

    if deployment is None:
        raise NoComparisonAvailable(
            f"no deploys recorded for {service!r}; "
            "run scripts/deploy.sh to populate the audit trail"
        )
    if not deployment.prev_commit_sha:
        raise NoComparisonAvailable(
            f"latest deploy of {service!r} ({deployment.commit_sha}) has no recorded predecessor, "
            "so there is no baseline to compare against"
        )
    if deployment.prev_commit_sha == deployment.commit_sha:
        raise NoComparisonAvailable(
            f"latest deploy of {service!r} redeployed the same commit "
            f"({deployment.commit_sha}); nothing changed to compare"
        )

    return ComparisonWindow(
        service=service,
        baseline_version=deployment.prev_commit_sha,
        candidate_version=deployment.commit_sha,
        onset_at=deployment.deployed_at,
        deployment_id=deployment.id,
    )
