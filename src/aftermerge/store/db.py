"""Engine and schema management for the audit trail."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from aftermerge.store.tables import Base

DEFAULT_DATABASE = "aftermerge"


def _dsn(database: str) -> str:
    user = os.environ.get("POSTGRES_USER", "aftermerge")
    password = os.environ.get("POSTGRES_PASSWORD", "aftermerge")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


def ensure_database(database: str = DEFAULT_DATABASE) -> None:
    """Create the AfterMerge database if it is missing.

    Postgres has no CREATE DATABASE IF NOT EXISTS, and it cannot run inside a
    transaction -- hence the explicit check and AUTOCOMMIT. Doing this in code
    rather than an initdb script means it also works on a Postgres volume that
    already exists, which is the common case here.
    """
    admin = create_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        admin.dispose()


def get_engine(database: str = DEFAULT_DATABASE) -> Engine:
    return create_engine(_dsn(database), pool_pre_ping=True)


def init_schema(engine: Engine | None = None) -> None:
    """Create tables. Alembic replaces this once slice 1 adds more of them."""
    Base.metadata.create_all(engine or get_engine())


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    factory = sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
