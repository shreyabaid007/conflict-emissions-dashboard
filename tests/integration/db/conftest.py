"""Fixtures for database integration tests using a PostgreSQL+PostGIS container.

Requires Docker to be running. The container is started once per test session
and torn down at the end. Each test function gets its own transaction that is
rolled back after the test, so tests don't interfere with each other.
"""
from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from wced.db.models import metadata

POSTGRES_IMAGE = "postgis/postgis:16-3.4"
CONTAINER_NAME = f"wced_test_pg_{uuid4().hex[:8]}"
HOST_PORT = 15432
PG_USER = "wced_test"
PG_PASS = "wced_test"
PG_DB = "wced_test"

DSN = f"postgresql+psycopg2://{PG_USER}:{PG_PASS}@localhost:{HOST_PORT}/{PG_DB}"


def _wait_for_pg(dsn: str, timeout: int = 30) -> sa.Engine:
    """Poll until PostgreSQL accepts connections or timeout."""
    engine = sa.create_engine(dsn, pool_pre_ping=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"PostgreSQL did not start within {timeout}s")


@pytest.fixture(scope="session")
def pg_engine():
    """Start a PostGIS container and return an engine connected to it.

    The container is removed after the test session completes.
    """
    import subprocess

    # Start container
    subprocess.run(
        [
            "docker", "run", "-d",
            "--name", CONTAINER_NAME,
            "-e", f"POSTGRES_USER={PG_USER}",
            "-e", f"POSTGRES_PASSWORD={PG_PASS}",
            "-e", f"POSTGRES_DB={PG_DB}",
            "-p", f"{HOST_PORT}:5432",
            POSTGRES_IMAGE,
        ],
        check=True,
        capture_output=True,
    )

    try:
        engine = _wait_for_pg(DSN)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        metadata.create_all(engine)
        yield engine
    finally:
        subprocess.run(
            ["docker", "rm", "-f", CONTAINER_NAME],
            capture_output=True,
        )


@pytest.fixture()
def db_session(pg_engine):
    """Provide a transactional session that rolls back after each test."""
    connection = pg_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()
