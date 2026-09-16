"""Deployment audit trail, against a real Postgres.

Runs against a throwaway database so it never touches the working audit trail.
Skipped when Postgres is unreachable, so `make test` stays useful without Docker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from aftermerge.store import db
from aftermerge.store.repositories import DeploymentRepository
from aftermerge.store.tables import Base

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


def test_ensure_database_is_idempotent() -> None:
    db.ensure_database(TEST_DATABASE)
    db.ensure_database(TEST_DATABASE)  # must not raise on the second call


def test_record_and_list(engine) -> None:
    with db.session_scope(engine) as session:
        DeploymentRepository(session).record(
            service="orders", commit_sha="aaa1111", prev_commit_sha="000aaaa"
        )

    with db.session_scope(engine) as session:
        rows = DeploymentRepository(session).list_recent(service="orders")

    assert len(rows) == 1
    assert rows[0].commit_sha == "aaa1111"
    assert rows[0].prev_commit_sha == "000aaaa"
    assert rows[0].deployed_at.tzinfo is not None, "timestamps must be timezone-aware"


def test_latest_before_finds_the_deploy_in_effect(engine) -> None:
    """The query change correlation runs against an incident's onset time."""
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    with db.session_scope(engine) as session:
        repo = DeploymentRepository(session)
        repo.record(service="orders", commit_sha="old", deployed_at=t0)
        repo.record(service="orders", commit_sha="new", deployed_at=t0 + timedelta(hours=2))

    with db.session_scope(engine) as session:
        repo = DeploymentRepository(session)
        assert repo.latest_before("orders", t0 + timedelta(hours=1)).commit_sha == "old"
        assert repo.latest_before("orders", t0 + timedelta(hours=3)).commit_sha == "new"
        assert repo.latest_before("orders", t0 - timedelta(hours=1)) is None


def test_rollbacks_are_recorded_not_collapsed(engine) -> None:
    """Redeploying a known SHA is a real event and must not be deduplicated."""
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    with db.session_scope(engine) as session:
        repo = DeploymentRepository(session)
        repo.record(service="orders", commit_sha="good", deployed_at=t0)
        repo.record(service="orders", commit_sha="bad", deployed_at=t0 + timedelta(minutes=10))
        repo.record(service="orders", commit_sha="good", deployed_at=t0 + timedelta(minutes=20))

    with db.session_scope(engine) as session:
        rows = DeploymentRepository(session).list_recent(service="orders")

    assert [r.commit_sha for r in rows] == ["good", "bad", "good"]


def test_services_are_isolated(engine) -> None:
    with db.session_scope(engine) as session:
        repo = DeploymentRepository(session)
        repo.record(service="orders", commit_sha="aaa")
        repo.record(service="gateway", commit_sha="bbb")

    with db.session_scope(engine) as session:
        repo = DeploymentRepository(session)
        assert len(repo.list_recent(service="orders")) == 1
        assert len(repo.list_recent()) == 2


def test_drop_and_create_leaves_a_usable_table(engine) -> None:
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM deployments")).scalar()
    assert count == 0
