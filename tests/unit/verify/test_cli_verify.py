"""CLI tests for ``wced verify`` subcommands.

Uses Typer's CliRunner so no real process is spawned. The queue is injected
via ``_inject_queue`` to keep tests hermetic — each test gets a fresh
InMemoryReviewQueue.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from wced.cli.main import app
from wced.cli.verify import _inject_queue
from wced.models.event import DetectionSource, EventStatus, FireEvent
from wced.models.provenance import ConfidenceLabel
from wced.verify.editorial import InMemoryReviewQueue

runner = CliRunner()


def _event(status: EventStatus = EventStatus.PENDING_REVIEW) -> FireEvent:
    now = datetime.now(tz=UTC)
    return FireEvent(
        facility_id=uuid4(),
        detected_at=now,
        last_seen_at=now,
        peak_frp_mw=80.0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.REPORTED,
        status=status,
        provenance_id=uuid4(),
        created_at=now,
        updated_at=now,
    )


def _fresh_queue(*events: FireEvent) -> InMemoryReviewQueue:
    q = InMemoryReviewQueue()
    for ev in events:
        q.submit(ev)
    return q


class TestPending:
    def test_empty_queue_message(self) -> None:
        _inject_queue(_fresh_queue())
        result = runner.invoke(app, ["verify", "pending"])
        assert result.exit_code == 0
        assert "No events pending" in result.output

    def test_lists_pending_events(self) -> None:
        ev = _event()
        _inject_queue(_fresh_queue(ev))
        result = runner.invoke(app, ["verify", "pending"])
        assert result.exit_code == 0
        assert str(ev.id) in result.output

    def test_does_not_list_published_events(self) -> None:
        ev = _event()
        q = _fresh_queue(ev)
        q.approve(ev.id, reviewer="r")
        _inject_queue(q)
        result = runner.invoke(app, ["verify", "pending"])
        assert "No events pending" in result.output


class TestShow:
    def test_shows_event_details(self) -> None:
        ev = _event()
        _inject_queue(_fresh_queue(ev))
        result = runner.invoke(app, ["verify", "show", str(ev.id)])
        assert result.exit_code == 0
        assert str(ev.id) in result.output
        assert "PENDING_REVIEW" in result.output

    def test_shows_editorial_history(self) -> None:
        ev = _event()
        q = _fresh_queue(ev)
        q.approve(ev.id, reviewer="analyst:jdoe", notes="Looks good.")
        _inject_queue(q)
        result = runner.invoke(app, ["verify", "show", str(ev.id)])
        assert result.exit_code == 0
        assert "APPROVED" in result.output
        assert "analyst:jdoe" in result.output

    def test_unknown_id_exits_nonzero(self) -> None:
        _inject_queue(_fresh_queue())
        result = runner.invoke(app, ["verify", "show", str(uuid4())])
        assert result.exit_code != 0


class TestApprove:
    def test_approve_publishes_event(self) -> None:
        ev = _event()
        q = _fresh_queue(ev)
        _inject_queue(q)
        result = runner.invoke(
            app, ["verify", "approve", str(ev.id), "--reviewer", "jdoe"]
        )
        assert result.exit_code == 0
        assert "PUBLISHED" in result.output
        assert q.get(ev.id).status is EventStatus.PUBLISHED

    def test_approve_with_notes(self) -> None:
        ev = _event()
        q = _fresh_queue(ev)
        _inject_queue(q)
        result = runner.invoke(
            app,
            ["verify", "approve", str(ev.id), "--reviewer", "jdoe", "--notes", "Verified via ACLED."],
        )
        assert result.exit_code == 0
        assert "Verified via ACLED." in result.output

    def test_approve_rejected_event_exits_nonzero(self) -> None:
        ev = _event()
        q = _fresh_queue(ev)
        q.reject(ev.id, reviewer="r", reason="bad data")
        _inject_queue(q)
        result = runner.invoke(
            app, ["verify", "approve", str(ev.id), "--reviewer", "r2"]
        )
        assert result.exit_code != 0
        assert "resubmit" in result.output.lower()

    def test_approve_unknown_id_exits_nonzero(self) -> None:
        _inject_queue(_fresh_queue())
        result = runner.invoke(
            app, ["verify", "approve", str(uuid4()), "--reviewer", "r"]
        )
        assert result.exit_code != 0


class TestReject:
    def test_reject_with_reason(self) -> None:
        ev = _event()
        q = _fresh_queue(ev)
        _inject_queue(q)
        result = runner.invoke(
            app,
            ["verify", "reject", str(ev.id), "--reviewer", "jdoe", "--reason", "Flaring artefact."],
        )
        assert result.exit_code == 0
        assert "Rejected" in result.output
        assert q.get(ev.id).status is EventStatus.REJECTED

    def test_reject_unknown_id_exits_nonzero(self) -> None:
        _inject_queue(_fresh_queue())
        result = runner.invoke(
            app,
            ["verify", "reject", str(uuid4()), "--reviewer", "r", "--reason", "x"],
        )
        assert result.exit_code != 0


class TestRetract:
    def test_retract_published_event(self) -> None:
        ev = _event()
        q = _fresh_queue(ev)
        q.approve(ev.id, reviewer="r")
        _inject_queue(q)
        result = runner.invoke(
            app,
            [
                "verify", "retract", str(ev.id),
                "--reviewer", "editor",
                "--reason", "Satellite imagery reanalysis showed a near-miss.",
            ],
        )
        assert result.exit_code == 0
        assert "Retracted" in result.output
        assert "changelog" in result.output.lower()
        assert q.get(ev.id).status is EventStatus.RETRACTED

    def test_retract_pending_event_exits_nonzero(self) -> None:
        ev = _event()
        q = _fresh_queue(ev)
        _inject_queue(q)
        result = runner.invoke(
            app,
            ["verify", "retract", str(ev.id), "--reviewer", "r", "--reason", "premature"],
        )
        assert result.exit_code != 0

    def test_retract_unknown_id_exits_nonzero(self) -> None:
        _inject_queue(_fresh_queue())
        result = runner.invoke(
            app,
            ["verify", "retract", str(uuid4()), "--reviewer", "r", "--reason", "x"],
        )
        assert result.exit_code != 0
