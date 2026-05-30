"""Database engine and session factory for WCED.

Provides a single ``get_engine()`` / ``get_session_factory()`` pair that the
rest of the application imports. Configuration is driven by the
``WCED_DATABASE_URL`` environment variable (PostgreSQL DSN).

Usage::

    from wced.db.session import get_engine, get_session_factory

    engine = get_engine()
    Session = get_session_factory(engine)
    with Session() as session:
        session.execute(...)
"""
from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg2://localhost:5432/wced"


@lru_cache(maxsize=1)
def get_engine(*, url: str | None = None, echo: bool = False) -> Engine:
    """Create or return a cached SQLAlchemy engine.

    Parameters
    ----------
    url : str or None
        PostgreSQL DSN. Falls back to ``WCED_DATABASE_URL`` env var, then
        to a localhost default.
    echo : bool
        Pass True to log all SQL statements (useful for debugging).
    """
    dsn = url or (
        os.environ.get("WCED_DB_DSN")
        or os.environ.get("WCED_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_DATABASE_URL
    )
    return create_engine(
        dsn,
        echo=echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Return a sessionmaker bound to *engine* (or the default engine)."""
    if engine is None:
        engine = get_engine()
    return sessionmaker(bind=engine, expire_on_commit=False)
