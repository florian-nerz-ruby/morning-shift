"""Database engine/session setup.

DATABASE_URL env var picks the backend - unset defaults to a local SQLite
file so nothing extra needs installing for local development. Point it at a
Postgres URL (e.g. postgresql+psycopg2://user:pass@host/dbname) in
production. Every store (RuleStore, PropertyStore, ThresholdRuleStore, the
handled-state functions) and both reservation loaders go through this same
engine/session, so switching backends is just setting the env var - none of
that code cares which database is actually behind it.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BASE_DIR = Path(__file__).parent
DEFAULT_SQLITE_PATH = BASE_DIR / "data" / "alfred.db"


class Base(DeclarativeBase):
    pass


def _default_database_url() -> str:
    DEFAULT_SQLITE_PATH.parent.mkdir(exist_ok=True)
    return f"sqlite:///{DEFAULT_SQLITE_PATH}"


DATABASE_URL = os.environ.get("DATABASE_URL") or _default_database_url()

# SQLite connections are single-threaded by default; the dev server (and
# waitress in production) can hand requests to different threads, and every
# store opens its own short-lived session per call rather than sharing one
# connection, so this just needs to be permitted, not actually shared unsafely.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Creates any tables that don't exist yet. Safe to call on every startup."""
    import models  # noqa: F401  (registers every model on Base.metadata)

    Base.metadata.create_all(engine)
