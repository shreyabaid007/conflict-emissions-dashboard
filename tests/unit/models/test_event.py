"""Tests for wced.models.event.

Covers:
- Enum membership (DetectionSource, EventStatus, ConfidenceLabel re-export)
- Construction defaults
- Temporal ordering constraints (last_seen_at >= detected_at;
  updated_at >= created_at)
- duration_hours computation
- is_persistent boundary behavior at the overpass threshold
- as_dict round-trip to JSON-friendly primitives
- Frozen-model behavior
- Hypothesis property test on persistence-vs-duration consistency
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from wced.models.event import (
    ConfidenceLabel,
    DetectionSource,
    EventStatus,
    FireEvent,
)
from wced.models.provenance import ConfidenceLabel as ProvenanceConfidenceLabel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_event(**kwargs: object) -> FireEvent:
    now = utcnow()
    defaults: dict[str, object] = dict(
        facility_id=uuid4(),
        detected_at=now,
        last_seen_at=now + timedelta(hours=18),
        peak_frp_mw=520.0,
        total_frp_integral_mj=4_800_000.0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.REPORTED,
        provenance_id=uuid4(),
        created_at=now,
        updated_at=now,
    )
    return FireEvent(**(defaults | kwargs))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestEnums:
    def test_confidence_label_is_reexport_of_provenance_enum(self) -> None:
        """The ConfidenceLabel imported from event must BE the provenance one.

        Re-exporting the same enum avoids two parallel hierarchies that
        could silently diverge.
        """
        assert ConfidenceLabel is ProvenanceConfidenceLabel

    def test_detection_source_values(self) -> None:
        names = {m.value for m in DetectionSource}
        assert names == {"FIRMS_VIIRS", "FIRMS_MODIS", "S2", "GEOSTATIONARY"}

    def test_event_status_values(self) -> None:
        names = {m.value for m in EventStatus}
        assert names == {"PENDING_REVIEW", "PUBLISHED", "REJECTED", "RETRACTED"}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestFireEventConstruction:
    def test_required_fields_with_defaults(self) -> None:
        e = make_event()
        assert isinstance(e.id, UUID)
        assert e.status is EventStatus.PENDING_REVIEW
        assert e.notes is None

    def test_explicit_status_accepted(self) -> None:
        e = make_event(status=EventStatus.PUBLISHED)
        assert e.status is EventStatus.PUBLISHED

    def test_fireevent_is_frozen(self) -> None:
        e = make_event()
        with pytest.raises(ValidationError):
            e.status = EventStatus.PUBLISHED  # type: ignore[misc]

    def test_negative_frp_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_event(peak_frp_mw=-1.0)
        with pytest.raises(ValidationError):
            make_event(total_frp_integral_mj=-0.001)

    def test_frp_integral_optional_at_first_detection(self) -> None:
        """Single-overpass events have no integral yet — must be constructable."""
        e = make_event(total_frp_integral_mj=None)
        assert e.total_frp_integral_mj is None

    def test_naive_detected_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_event(detected_at=datetime(2026, 3, 1))


# ---------------------------------------------------------------------------
# Temporal ordering
# ---------------------------------------------------------------------------


class TestTemporalOrdering:
    def test_last_seen_before_detected_rejected(self) -> None:
        now = utcnow()
        with pytest.raises(ValidationError) as excinfo:
            make_event(detected_at=now, last_seen_at=now - timedelta(hours=1))
        assert "last_seen_at" in str(excinfo.value)

    def test_last_seen_equal_to_detected_allowed(self) -> None:
        """Single-overpass detections are constructable but non-persistent."""
        now = utcnow()
        e = make_event(detected_at=now, last_seen_at=now)
        assert e.duration_hours == 0.0
        assert not e.is_persistent

    def test_updated_at_before_created_at_rejected(self) -> None:
        now = utcnow()
        with pytest.raises(ValidationError) as excinfo:
            make_event(created_at=now, updated_at=now - timedelta(seconds=1))
        assert "updated_at" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Derived properties
# ---------------------------------------------------------------------------


class TestDurationHours:
    @pytest.mark.parametrize(
        ("delta", "expected_hours"),
        [
            (timedelta(seconds=0), 0.0),
            (timedelta(hours=1), 1.0),
            (timedelta(hours=12), 12.0),
            (timedelta(hours=36, minutes=30), 36.5),
            (timedelta(days=3), 72.0),
        ],
    )
    def test_duration_matches_delta(
        self, delta: timedelta, expected_hours: float
    ) -> None:
        now = utcnow()
        e = make_event(detected_at=now, last_seen_at=now + delta)
        assert e.duration_hours == pytest.approx(expected_hours)


class TestIsPersistent:
    def test_single_overpass_not_persistent(self) -> None:
        now = utcnow()
        e = make_event(detected_at=now, last_seen_at=now)
        assert e.is_persistent is False

    def test_two_overpasses_persistent(self) -> None:
        """Any strictly-positive duration counts as ≥2 overpasses."""
        now = utcnow()
        # Even a small interval indicates a re-observation occurred.
        e = make_event(detected_at=now, last_seen_at=now + timedelta(minutes=1))
        assert e.is_persistent is True

    def test_long_duration_persistent(self) -> None:
        now = utcnow()
        e = make_event(detected_at=now, last_seen_at=now + timedelta(days=2))
        assert e.is_persistent is True


# ---------------------------------------------------------------------------
# as_dict
# ---------------------------------------------------------------------------


class TestAsDict:
    def test_as_dict_uses_json_primitives(self) -> None:
        e = make_event()
        d = e.as_dict()
        # UUIDs become strings
        assert isinstance(d["id"], str)
        assert UUID(d["id"]) == e.id
        assert isinstance(d["facility_id"], str)
        # Datetimes become ISO strings
        assert isinstance(d["detected_at"], str)
        assert d["detected_at"].startswith(str(e.detected_at.year))
        # Enums become their string values
        assert d["detection_source"] == "FIRMS_VIIRS"
        assert d["status"] == "PENDING_REVIEW"
        assert d["confidence_label"] == "REPORTED"

    def test_as_dict_roundtrips_through_constructor(self) -> None:
        e = make_event(notes="cloud-obscured between overpasses 3-5")
        d = e.as_dict()
        rebuilt = FireEvent.model_validate(d)
        assert rebuilt == e


# ---------------------------------------------------------------------------
# Property-based: is_persistent ⇔ duration_hours > 0
# ---------------------------------------------------------------------------


class TestPersistenceProperty:
    @settings(max_examples=200, deadline=None)
    @given(
        offset_seconds=st.integers(min_value=0, max_value=14 * 24 * 3600),
    )
    def test_persistence_matches_strict_positive_duration(
        self, offset_seconds: int
    ) -> None:
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        e = make_event(
            detected_at=now,
            last_seen_at=now + timedelta(seconds=offset_seconds),
        )
        assert e.is_persistent == (offset_seconds > 0)
        assert e.duration_hours == pytest.approx(offset_seconds / 3600.0)
