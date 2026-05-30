"""Tests for the recompute_runs table and repository.

Covers:
- Opening a run creates a RUNNING row
- Closing a run updates status, finished_at, events_affected
- Get returns the correct state at each stage
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from wced.db.repositories.recompute import RecomputeRunRepository


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE recompute_runs (
                id TEXT PRIMARY KEY,
                methodology_version TEXT NOT NULL,
                date_range_start TIMESTAMP,
                date_range_end TIMESTAMP,
                initiator TEXT NOT NULL,
                trigger TEXT NOT NULL,
                events_affected INTEGER,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                status TEXT NOT NULL
            )
        """))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


class TestRecomputeRunLifecycle:
    def test_open_run_creates_running_row(self, db_session) -> None:
        repo = RecomputeRunRepository(db_session)
        run_id = uuid4()
        now = datetime.now(tz=UTC)
        repo.open_run(
            id=run_id,
            methodology_version="1.0.5",
            date_range_start=None,
            date_range_end=None,
            initiator="analyst:jdoe",
            trigger="cli",
            started_at=now,
        )
        db_session.commit()
        row = repo.get(run_id)
        assert row is not None
        assert row["status"] == "RUNNING"
        assert row["finished_at"] is None
        assert row["events_affected"] is None

    def test_close_run_sets_finished_and_count(self, db_session) -> None:
        repo = RecomputeRunRepository(db_session)
        run_id = uuid4()
        start = datetime(2026, 5, 30, 10, 0, tzinfo=UTC)
        repo.open_run(
            id=run_id,
            methodology_version="1.0.5",
            date_range_start=None,
            date_range_end=None,
            initiator="analyst:jdoe",
            trigger="cli",
            started_at=start,
        )
        finish = datetime(2026, 5, 30, 10, 15, tzinfo=UTC)
        repo.close_run(
            run_id,
            status="COMPLETED",
            finished_at=finish,
            events_affected=42,
        )
        db_session.commit()
        row = repo.get(run_id)
        assert row["status"] == "COMPLETED"
        assert row["events_affected"] == 42
        assert row["finished_at"] is not None

    def test_close_run_with_failure(self, db_session) -> None:
        repo = RecomputeRunRepository(db_session)
        run_id = uuid4()
        now = datetime.now(tz=UTC)
        repo.open_run(
            id=run_id, methodology_version="1.1.0",
            date_range_start=None, date_range_end=None,
            initiator="system", trigger="scheduled",
            started_at=now,
        )
        repo.close_run(
            run_id, status="FAILED",
            finished_at=datetime.now(tz=UTC),
            events_affected=0,
        )
        db_session.commit()
        row = repo.get(run_id)
        assert row["status"] == "FAILED"

    def test_date_range_persisted(self, db_session) -> None:
        repo = RecomputeRunRepository(db_session)
        run_id = uuid4()
        start_range = datetime(2026, 3, 1, tzinfo=UTC)
        end_range = datetime(2026, 5, 30, tzinfo=UTC)
        repo.open_run(
            id=run_id, methodology_version="1.0.5",
            date_range_start=start_range, date_range_end=end_range,
            initiator="analyst", trigger="cli",
            started_at=datetime.now(tz=UTC),
        )
        db_session.commit()
        row = repo.get(run_id)
        assert row["date_range_start"] is not None
        assert row["date_range_end"] is not None

    def test_list_recent_returns_newest_first(self, db_session) -> None:
        repo = RecomputeRunRepository(db_session)
        t1 = datetime(2026, 5, 1, tzinfo=UTC)
        t2 = datetime(2026, 5, 2, tzinfo=UTC)
        repo.open_run(
            id=uuid4(), methodology_version="1.0.5",
            date_range_start=None, date_range_end=None,
            initiator="a", trigger="cli", started_at=t1,
        )
        repo.open_run(
            id=uuid4(), methodology_version="1.0.5",
            date_range_start=None, date_range_end=None,
            initiator="b", trigger="cli", started_at=t2,
        )
        db_session.commit()
        recent = repo.list_recent(limit=10)
        assert recent[0]["started_at"] >= recent[1]["started_at"]

    def test_get_nonexistent_returns_none(self, db_session) -> None:
        repo = RecomputeRunRepository(db_session)
        assert repo.get(uuid4()) is None
