"""AfterMerge's durable audit trail.

Deliberately a **separate database** from the demo app's. shopdemo is the system
under test: later slices wipe and reseed it, and the reproducer spins up throwaway
copies. AfterMerge's own record of what it observed must outlive all of that.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
