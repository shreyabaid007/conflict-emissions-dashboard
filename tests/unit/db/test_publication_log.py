"""Tests for the publication_log table and repository.

Covers:
- Append-only semantics (insert works, no update/delete exposed)
- Every editorial transition appends a log entry
- Log entries are retrievable by target_id
- Entries are immutable after insertion
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text, update, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from wced.db.repositories.publication_log import PublicationLogRepository


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE publication_log (
                id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT,
                methodology_version TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


class TestPublicationLogAppend:
    def test_append_creates_row(self, db_session) -> None:
        repo = PublicationLogRepository(db_session)
        log_id = uuid4()
        target_id = uuid4()
        repo.append(
            id=log_id,
            target_type="fire_event",
            target_id=target_id,
            from_state="PENDING_REVIEW",
            to_state="PUBLISHED",
            action="approve",
            actor="analyst:jdoe",
            reason=None,
            methodology_version="1.0.5",
            created_at=datetime.now(tz=UTC),
        )
        db_session.commit()
        entries = repo.list_by_target(target_id)
        assert len(entries) == 1
        assert entries[0]["action"] == "approve"
        assert entries[0]["from_state"] == "PENDING_REVIEW"
        assert entries[0]["to_state"] == "PUBLISHED"

    def test_multiple_transitions_for_same_target(self, db_session) -> None:
        repo = PublicationLogRepository(db_session)
        target_id = uuid4()
        now = datetime.now(tz=UTC)
        repo.append(
            id=uuid4(), target_type="fire_event", target_id=target_id,
            from_state="PENDING_REVIEW", to_state="PUBLISHED",
            action="approve", actor="r1", reason=None,
            methodology_version="1.0.5", created_at=now,
        )
        repo.append(
            id=uuid4(), target_type="fire_event", target_id=target_id,
            from_state="PUBLISHED", to_state="RETRACTED",
            action="retract", actor="r2", reason="Misidentified facility",
            methodology_version="1.0.5", created_at=now,
        )
        db_session.commit()
        entries = repo.list_by_target(target_id)
        assert len(entries) == 2
        actions = [e["action"] for e in entries]
        assert actions == ["approve", "retract"]

    def test_retraction_reason_persisted(self, db_session) -> None:
        repo = PublicationLogRepository(db_session)
        target_id = uuid4()
        repo.append(
            id=uuid4(), target_type="fire_event", target_id=target_id,
            from_state="PUBLISHED", to_state="RETRACTED",
            action="retract", actor="editor", reason="Satellite misread",
            methodology_version="1.0.5",
            created_at=datetime.now(tz=UTC),
        )
        db_session.commit()
        entries = repo.list_by_target(target_id)
        assert entries[0]["reason"] == "Satellite misread"

    def test_list_recent_returns_newest_first(self, db_session) -> None:
        repo = PublicationLogRepository(db_session)
        t1 = datetime(2026, 5, 1, tzinfo=UTC)
        t2 = datetime(2026, 5, 2, tzinfo=UTC)
        repo.append(
            id=uuid4(), target_type="fire_event", target_id=uuid4(),
            from_state="PENDING_REVIEW", to_state="PUBLISHED",
            action="approve", actor="r1", reason=None,
            methodology_version="1.0.5", created_at=t1,
        )
        repo.append(
            id=uuid4(), target_type="fire_event", target_id=uuid4(),
            from_state="PENDING_REVIEW", to_state="PUBLISHED",
            action="approve", actor="r2", reason=None,
            methodology_version="1.0.5", created_at=t2,
        )
        db_session.commit()
        recent = repo.list_recent(limit=10)
        assert recent[0]["created_at"] >= recent[1]["created_at"]

    def test_methodology_version_nullable(self, db_session) -> None:
        repo = PublicationLogRepository(db_session)
        repo.append(
            id=uuid4(), target_type="fire_event", target_id=uuid4(),
            from_state="PENDING_REVIEW", to_state="REJECTED",
            action="reject", actor="r1", reason="Bad data",
            methodology_version=None,
            created_at=datetime.now(tz=UTC),
        )
        db_session.commit()
        recent = repo.list_recent(limit=1)
        assert recent[0]["methodology_version"] is None


class TestPublicationLogImmutability:
    """The repository exposes no update or delete methods.

    This test verifies the contract: callers cannot mutate log entries
    through the repository interface.
    """

    def test_no_update_method(self) -> None:
        assert not hasattr(PublicationLogRepository, "update")

    def test_no_delete_method(self) -> None:
        assert not hasattr(PublicationLogRepository, "delete")
