"""Tests for wced.verify.acled_corroboration.

Spatial/temporal matching is deterministic, so all tests run without network
I/O using hand-crafted ACLEDEvent and CandidateFireEvent fixtures.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection
from wced.ingest.acled import ACLEDEvent
from wced.models.event import DetectionSource
from wced.verify.acled_corroboration import (
    DEFAULT_SPACE_WINDOW_M,
    DEFAULT_TIME_WINDOW_H,
    _haversine_m,
    find_acled_corroboration,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _acled_event(
    lat: float = 32.66,
    lon: float = 51.68,
    event_date: date | None = None,
    event_id: str = "IRN5023",
) -> ACLEDEvent:
    d = event_date or date(2026, 3, 7)
    detected_at = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return ACLEDEvent(
        event_id_cnty=event_id,
        event_date=d,
        event_type="Explosions/Remote violence",
        sub_event_type="Air/drone strike",
        actor1="Military Forces of Israel",
        actor2="Government of Iran",
        country="Iran",
        location="Isfahan",
        latitude=lat,
        longitude=lon,
        source="Tehran Times",
        notes="Strike on oil refinery.",
        fatalities=0,
        timestamp=1741737600,
        iso=364,
        detected_at=detected_at,
    )


def _candidate(
    lat: float = 32.66,
    lon: float = 51.68,
    detected_at: datetime | None = None,
    n_overpasses: int = 1,
) -> CandidateFireEvent:
    t = detected_at or datetime(2026, 3, 7, 9, 0, tzinfo=UTC)
    h = FIRMSDetection(
        latitude=lat,
        longitude=lon,
        frp_mw=80.0,
        detected_at=t,
        detection_source=DetectionSource.FIRMS_VIIRS,
        brightness_k=380.0,
        confidence="h",
        source_id=uuid4(),
    )
    return CandidateFireEvent(
        hotspots=(h,),
        centroid_lat=lat,
        centroid_lon=lon,
        first_detected_at=t,
        last_detected_at=t,
        peak_frp_mw=80.0,
        mean_frp_mw=80.0,
        n_overpasses=n_overpasses,
        provenance_id=uuid4(),
    )


# ---------------------------------------------------------------------------
# Haversine helper
# ---------------------------------------------------------------------------


class TestHaversine:
    def test_same_point_is_zero(self) -> None:
        assert _haversine_m(32.66, 51.68, 32.66, 51.68) == pytest.approx(0.0)

    def test_known_distance(self) -> None:
        # Tehran (35.69, 51.39) to Isfahan (32.66, 51.68) ≈ 340 km.
        d = _haversine_m(35.69, 51.39, 32.66, 51.68)
        assert 330_000 < d < 350_000

    def test_symmetric(self) -> None:
        d1 = _haversine_m(32.0, 51.0, 33.0, 52.0)
        d2 = _haversine_m(33.0, 52.0, 32.0, 51.0)
        assert d1 == pytest.approx(d2)


# ---------------------------------------------------------------------------
# Temporal matching
# ---------------------------------------------------------------------------


class TestTemporalMatching:
    def test_candidate_on_same_day_matches(self) -> None:
        event = _acled_event(event_date=date(2026, 3, 7))
        # Candidate at 09:00 on the same day — inside [midnight-24h, midnight+48h].
        cand = _candidate(detected_at=datetime(2026, 3, 7, 9, 0, tzinfo=UTC))
        matches = find_acled_corroboration(cand, [event])
        assert event in matches

    def test_candidate_end_of_same_day_matches(self) -> None:
        event = _acled_event(event_date=date(2026, 3, 7))
        # 23:59 on the event day: inside [midnight-24h, midnight+48h].
        cand = _candidate(detected_at=datetime(2026, 3, 7, 23, 59, tzinfo=UTC))
        matches = find_acled_corroboration(cand, [event])
        assert event in matches

    def test_candidate_within_extension_before_event_matches(self) -> None:
        event = _acled_event(event_date=date(2026, 3, 7))
        # 12 hours before midnight — inside the 24 h pre-extension.
        cand = _candidate(detected_at=datetime(2026, 3, 6, 12, 0, tzinfo=UTC))
        matches = find_acled_corroboration(cand, [event])
        assert event in matches

    def test_candidate_after_extension_end_does_not_match(self) -> None:
        event = _acled_event(event_date=date(2026, 3, 7))
        # 25 h after midnight of event_date + 24 h (event window end) = 49 h out.
        cand = _candidate(detected_at=datetime(2026, 3, 9, 1, 0, tzinfo=UTC))
        matches = find_acled_corroboration(cand, [event])
        assert event not in matches

    def test_candidate_well_before_event_does_not_match(self) -> None:
        event = _acled_event(event_date=date(2026, 3, 7))
        # Three days before midnight-24h boundary.
        cand = _candidate(detected_at=datetime(2026, 3, 3, 0, 0, tzinfo=UTC))
        matches = find_acled_corroboration(cand, [event])
        assert event not in matches

    def test_empty_event_list_returns_empty(self) -> None:
        cand = _candidate()
        matches = find_acled_corroboration(cand, [])
        assert matches == []


# ---------------------------------------------------------------------------
# Spatial matching
# ---------------------------------------------------------------------------


class TestSpatialMatching:
    def test_exact_location_matches(self) -> None:
        event = _acled_event(lat=32.66, lon=51.68)
        cand = _candidate(lat=32.66, lon=51.68)
        matches = find_acled_corroboration(cand, [event])
        assert event in matches

    def test_event_beyond_space_window_excluded(self) -> None:
        # Move event ~5 km north of candidate.
        event = _acled_event(lat=32.705, lon=51.68)  # ≈5 km away
        cand = _candidate(lat=32.66, lon=51.68)
        matches = find_acled_corroboration(
            cand, [event], space_window_m=2_000.0
        )
        assert event not in matches

    def test_event_within_custom_space_window_included(self) -> None:
        event = _acled_event(lat=32.705, lon=51.68)  # ≈5 km
        cand = _candidate(lat=32.66, lon=51.68)
        matches = find_acled_corroboration(
            cand, [event], space_window_m=6_000.0
        )
        assert event in matches

    def test_results_sorted_ascending_by_distance(self) -> None:
        close = _acled_event(lat=32.661, lon=51.680, event_id="IRN001")  # ~110 m
        far = _acled_event(lat=32.672, lon=51.680, event_id="IRN002")   # ~1.3 km
        cand = _candidate(lat=32.66, lon=51.68)
        matches = find_acled_corroboration(
            cand, [far, close], space_window_m=5_000.0
        )
        assert matches == [close, far]


# ---------------------------------------------------------------------------
# Combined temporal + spatial gating
# ---------------------------------------------------------------------------


class TestCombinedGating:
    def test_both_criteria_must_hold(self) -> None:
        # Temporally close but spatially too far.
        distant = _acled_event(lat=33.0, lon=51.68, event_id="DIST")
        # Spatially close but temporally too far.
        old = _acled_event(
            lat=32.66, lon=51.68,
            event_date=date(2026, 2, 1),
            event_id="OLD",
        )
        cand = _candidate(
            lat=32.66, lon=51.68,
            detected_at=datetime(2026, 3, 7, 9, 0, tzinfo=UTC),
        )
        matches = find_acled_corroboration(cand, [distant, old])
        assert matches == []

    def test_multiple_matches_returned(self) -> None:
        ev1 = _acled_event(lat=32.661, lon=51.680, event_id="EV1")
        ev2 = _acled_event(lat=32.662, lon=51.680, event_id="EV2")
        cand = _candidate(lat=32.66, lon=51.68)
        matches = find_acled_corroboration(cand, [ev1, ev2])
        assert len(matches) == 2

    def test_custom_time_window_respected(self) -> None:
        event = _acled_event(event_date=date(2026, 3, 7))
        # 3 h before midnight — inside 6 h window but outside 2 h window.
        cand = _candidate(detected_at=datetime(2026, 3, 6, 21, 0, tzinfo=UTC))

        inside = find_acled_corroboration(cand, [event], time_window_h=6.0)
        outside = find_acled_corroboration(cand, [event], time_window_h=2.0)
        assert event in inside
        assert event not in outside
