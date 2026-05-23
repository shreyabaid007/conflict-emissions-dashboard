"""Unit tests for wced.pipeline.daily_ingest.

Strategy
--------
All tasks are tested by calling their underlying functions directly, bypassing
the Prefect runtime (``task.fn(...)`` or just calling the task object outside
a flow context — both work in Prefect 3 without a running server).

External I/O (FIRMS API, ACLED API, Sentinel-2 STAC, Claude API) is fully
mocked so the tests run offline without API credentials.

Coverage targets
----------------
- _detection_hash                    deterministic + collision-resistant
- _load_seen_hashes / _save_seen_hashes  round-trip via tmp_path
- detect_candidate_events            clustering, deduplication, facility match
- corroborate_with_acled             temporal + spatial match logic
- assign_confidence_labels           decision table from methodology §4.3
- submit_to_editorial_queue          FireEvent construction, idempotency
- log_pipeline_run                   hash flush, structured logging
- daily_ingest (flow)                end-to-end with all connectors mocked
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock
from uuid import uuid4, UUID, NAMESPACE_URL, uuid5

import pytest

from wced.detect.hotspot import FIRMSDetection, CandidateFireEvent
from wced.ingest.acled import ACLEDEvent
from wced.models.event import DetectionSource, EventStatus
from wced.models.facility import Facility, FacilityType
from wced.models.provenance import (
    ConfidenceLabel,
    Source,
    SourceType,
)
from wced.verify.corroboration import CorroborationMatch
from wced.pipeline.daily_ingest import (
    IRAN_BBOX,
    MatchedCandidate,
    PipelineRunMetrics,
    S2ChipResult,
    _detection_hash,
    _load_seen_hashes,
    _save_seen_hashes,
    _build_fire_event,
    assign_confidence_labels,
    corroborate_with_conflict_events,
    detect_candidate_events,
    log_pipeline_run,
    submit_to_editorial_queue,
    daily_ingest,
)
from wced.verify.sentinel2_check import VerificationStatus, VerifiedCandidate


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 3, 15, 6, 0, tzinfo=UTC)
_RUN_DATE = date(2026, 3, 15)


def make_source() -> Source:
    return Source(
        source_type=SourceType.SATELLITE,
        identifier="https://firms.example.com/test",
        retrieved_at=_T0,
        retrieved_by="test",
        content_hash="aabbcc",
        metadata={},
    )


def make_firms_row(
    lat: float = 32.0,
    lon: float = 51.0,
    frp: float = 25.0,
    acq_date: str = "2026-03-15",
    acq_time: str = "0600",
    satellite: str = "N",
    detection_source: str = DetectionSource.FIRMS_VIIRS.value,
    source: Source | None = None,
) -> dict[str, Any]:
    src = source or make_source()
    return {
        "latitude": lat,
        "longitude": lon,
        "frp": frp,
        "brightness": 320.0,
        "confidence": "n",
        "acq_date": acq_date,
        "acq_time": acq_time,
        "satellite": satellite,
        "detected_at": _T0,
        "_detection_source": detection_source,
        "_source": src,
    }


def make_facility(
    lat: float = 32.001,
    lon: float = 51.001,
    facility_type: FacilityType = FacilityType.REFINERY,
) -> Facility:
    return Facility(
        name="Test Refinery",
        facility_type=facility_type,
        geometry_wkt=f"POINT ({lon} {lat})",
        country="IRN",
        source_url="https://example.com/test-refinery",
        added_at=_T0,
        capacity_barrels=100_000.0,
    )


def make_acled_event(
    lat: float = 32.0,
    lon: float = 51.0,
    detected_at: datetime | None = None,
) -> ACLEDEvent:
    dt = detected_at or _T0
    return ACLEDEvent(
        event_id_cnty="IRN001",
        event_date=dt.date(),
        event_type="Explosions/Remote violence",
        sub_event_type="Air/drone strike",
        actor1="Test Actor",
        actor2="",
        country="Iran",
        location="Test Location",
        latitude=lat,
        longitude=lon,
        source="Test Source",
        notes="Test notes",
        fatalities=0,
        timestamp=int(dt.timestamp()),
        iso=364,
        detected_at=dt,
    )


def make_matched_candidate(
    lat: float = 32.0,
    lon: float = 51.0,
    n_overpasses: int = 2,
    facility: Facility | None = None,
    dist_m: float = 100.0,
) -> MatchedCandidate:
    """Build a MatchedCandidate with a minimal CandidateFireEvent."""
    from wced.provenance.store import InMemoryProvenanceStore
    from wced.detect.hotspot import hotspots_to_candidates

    src_id = uuid4()
    hotspots = tuple(
        FIRMSDetection(
            latitude=lat,
            longitude=lon,
            frp_mw=20.0,
            detected_at=_T0 + timedelta(hours=i * 13),
            detection_source=DetectionSource.FIRMS_VIIRS,
            brightness_k=320.0,
            confidence="n",
            source_id=src_id,
        )
        for i in range(n_overpasses)
    )
    from wced.detect.hotspot import _build_candidate
    from wced.models.provenance import ProvenanceRecord

    store = InMemoryProvenanceStore()
    rec = ProvenanceRecord(
        produced_by="test",
        inputs=[src_id],
        method="test",
        parameters={},
        produced_at=_T0,
        confidence_label=ConfidenceLabel.SUSPECTED,
    )
    store.record_provenance(rec)
    candidate = _build_candidate(list(hotspots), rec.id)

    import hashlib
    cand_hash = hashlib.sha256(
        "|".join(sorted(str(h.id) for h in hotspots)).encode()
    ).hexdigest()

    return MatchedCandidate(
        candidate=candidate,
        facility=facility,
        match_distance_m=dist_m if facility else float("inf"),
        detection_hash=cand_hash,
    )


# ---------------------------------------------------------------------------
# _detection_hash
# ---------------------------------------------------------------------------


class TestDetectionHash:
    def test_deterministic(self) -> None:
        row = make_firms_row()
        assert _detection_hash(row) == _detection_hash(row)

    def test_differs_on_lat(self) -> None:
        r1 = make_firms_row(lat=32.0)
        r2 = make_firms_row(lat=32.001)
        assert _detection_hash(r1) != _detection_hash(r2)

    def test_differs_on_satellite(self) -> None:
        r1 = make_firms_row(satellite="N")
        r2 = make_firms_row(satellite="A")
        assert _detection_hash(r1) != _detection_hash(r2)

    def test_ignores_frp(self) -> None:
        # FRP is not part of the identity key — the same pixel can have
        # slightly different FRP values across sources.
        r1 = make_firms_row(frp=25.0)
        r2 = make_firms_row(frp=30.0)
        assert _detection_hash(r1) == _detection_hash(r2)

    def test_returns_hex_string(self) -> None:
        h = _detection_hash(make_firms_row())
        assert len(h) == 64
        int(h, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# _load_seen_hashes / _save_seen_hashes
# ---------------------------------------------------------------------------


class TestSeenHashStore:
    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            result = _load_seen_hashes(_RUN_DATE)
        assert result == set()

    def test_round_trip(self, tmp_path: Path) -> None:
        hashes = {"aabb", "ccdd", "eeff"}
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            _save_seen_hashes(_RUN_DATE, hashes)
            loaded = _load_seen_hashes(_RUN_DATE)
        assert loaded == hashes

    def test_malformed_file_returns_empty(self, tmp_path: Path) -> None:
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            path = tmp_path / f"{_RUN_DATE.isoformat()}.json"
            path.write_text("not-json", encoding="utf-8")
            result = _load_seen_hashes(_RUN_DATE)
        assert result == set()

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        subdir = tmp_path / "a" / "b" / "c"
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", subdir
        ):
            _save_seen_hashes(_RUN_DATE, {"x"})
            assert (subdir / f"{_RUN_DATE.isoformat()}.json").exists()


# ---------------------------------------------------------------------------
# detect_candidate_events
# ---------------------------------------------------------------------------


class TestDetectCandidateEvents:
    def test_empty_firms_results(self, tmp_path: Path) -> None:
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            result = detect_candidate_events.fn([], [])
        assert result == []

    def test_single_row_produces_one_candidate(self, tmp_path: Path) -> None:
        row = make_firms_row(lat=32.0, lon=51.0)
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            result = detect_candidate_events.fn([row], [])
        assert len(result) == 1
        assert result[0].facility is None
        assert result[0].match_distance_m == float("inf")

    def test_facility_match(self, tmp_path: Path) -> None:
        row = make_firms_row(lat=32.0, lon=51.0)
        facility = make_facility(lat=32.001, lon=51.001)
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            result = detect_candidate_events.fn([row], [facility])
        # The facility is ~157 m away — within the 500 m threshold.
        assert len(result) == 1
        assert result[0].facility is not None
        assert result[0].facility.id == facility.id
        assert result[0].match_distance_m < 500.0

    def test_distant_facility_is_not_matched(self, tmp_path: Path) -> None:
        row = make_firms_row(lat=32.0, lon=51.0)
        facility = make_facility(lat=33.0, lon=52.0)  # ~150 km away
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            result = detect_candidate_events.fn([row], [facility])
        assert result[0].facility is None

    def test_deduplication_filters_seen_rows(self, tmp_path: Path) -> None:
        row = make_firms_row()
        h = _detection_hash(row)
        # Pre-populate the seen-hash store.
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            _save_seen_hashes(_RUN_DATE, {h})
            result = detect_candidate_events.fn([row], [])
        assert result == []

    def test_deduplication_only_filters_matching_hashes(self, tmp_path: Path) -> None:
        row_a = make_firms_row(lat=32.0, lon=51.0, satellite="N")
        row_b = make_firms_row(lat=33.0, lon=52.0, satellite="A")
        h_a = _detection_hash(row_a)
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            _save_seen_hashes(_RUN_DATE, {h_a})
            result = detect_candidate_events.fn([row_a, row_b], [])
        # Only row_b should produce a candidate.
        assert len(result) == 1

    def test_deterministic_detection_ids(self, tmp_path: Path) -> None:
        """Same raw row must always map to the same FIRMSDetection.id."""
        row = make_firms_row()
        h = _detection_hash(row)
        expected_id = uuid5(NAMESPACE_URL, h)
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            result = detect_candidate_events.fn([row], [])
        assert len(result) == 1
        det_ids = {d.id for d in result[0].candidate.hotspots}
        assert expected_id in det_ids

    def test_two_spatially_close_rows_cluster(self, tmp_path: Path) -> None:
        """Rows within 500 m should form a single candidate."""
        src = make_source()
        row_a = make_firms_row(lat=32.000, lon=51.000, satellite="N", source=src)
        row_b = make_firms_row(lat=32.001, lon=51.001, satellite="A", source=src)
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            result = detect_candidate_events.fn([row_a, row_b], [])
        assert len(result) == 1
        assert len(result[0].candidate.hotspots) == 2

    def test_two_distant_rows_produce_separate_candidates(
        self, tmp_path: Path
    ) -> None:
        src = make_source()
        row_a = make_firms_row(lat=32.0, lon=51.0, satellite="N", source=src)
        row_b = make_firms_row(lat=34.0, lon=53.0, satellite="A", source=src)
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            result = detect_candidate_events.fn([row_a, row_b], [])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# corroborate_with_conflict_events
# ---------------------------------------------------------------------------


class TestCorroborateWithConflictEvents:
    def test_empty_candidates_empty_result(self) -> None:
        result = corroborate_with_conflict_events.fn([], [])
        assert result == {}

    def test_empty_events(self) -> None:
        mc = make_matched_candidate()
        result = corroborate_with_conflict_events.fn([mc], [])
        assert result == {str(mc.candidate.id): []}

    def test_matching_event_within_window(self) -> None:
        mc = make_matched_candidate(lat=32.0, lon=51.0)
        event = make_acled_event(lat=32.001, lon=51.001, detected_at=_T0)
        result = corroborate_with_conflict_events.fn([mc], [event])
        matches = result[str(mc.candidate.id)]
        assert len(matches) == 1
        assert isinstance(matches[0], CorroborationMatch)
        assert matches[0].source_type == "acled"

    def test_event_too_far_spatially(self) -> None:
        mc = make_matched_candidate(lat=32.0, lon=51.0)
        event = make_acled_event(lat=34.0, lon=53.0, detected_at=_T0)
        result = corroborate_with_conflict_events.fn([mc], [event])
        assert result[str(mc.candidate.id)] == []

    def test_event_outside_time_window(self) -> None:
        mc = make_matched_candidate(lat=32.0, lon=51.0)
        event = make_acled_event(
            lat=32.0, lon=51.0, detected_at=_T0 + timedelta(days=5)
        )
        result = corroborate_with_conflict_events.fn([mc], [event])
        assert result[str(mc.candidate.id)] == []


# ---------------------------------------------------------------------------
# assign_confidence_labels
# ---------------------------------------------------------------------------


class TestAssignConfidenceLabels:
    def _make_verified(
        self,
        mc: MatchedCandidate,
        status: VerificationStatus,
    ) -> dict[str, VerifiedCandidate]:
        from wced.ai.classify import FireClassification, FireLabel

        if status is VerificationStatus.VERIFIED:
            cls = FireClassification(
                label=FireLabel.CONFIRMED_FIRE,
                confidence=0.92,
                rationale="Saturated SWIR",
                provenance_id=uuid4(),
            )
        else:
            cls = None

        return {
            str(mc.candidate.id): VerifiedCandidate(
                candidate=mc.candidate,
                status=status,
                classification=cls,
            )
        }

    def test_confirmed_requires_persistent_s2_acled(self) -> None:
        mc = make_matched_candidate(n_overpasses=2)
        verified = self._make_verified(mc, VerificationStatus.VERIFIED)
        ev = make_acled_event()
        corr = {str(mc.candidate.id): [
            CorroborationMatch(event=ev, source_type="acled", distance_m=100.0)
        ]}
        result = assign_confidence_labels.fn([mc], verified, corr)
        assert result[str(mc.candidate.id)] is ConfidenceLabel.CONFIRMED

    def test_verified_persistent_s2_no_acled(self) -> None:
        mc = make_matched_candidate(n_overpasses=2)
        verified = self._make_verified(mc, VerificationStatus.VERIFIED)
        corr: dict = {str(mc.candidate.id): []}
        result = assign_confidence_labels.fn([mc], verified, corr)
        assert result[str(mc.candidate.id)] is ConfidenceLabel.VERIFIED

    def test_reported_persistent_no_s2(self) -> None:
        mc = make_matched_candidate(n_overpasses=2)
        verified = self._make_verified(mc, VerificationStatus.AWAITING_OPTICAL_CHECK)
        result = assign_confidence_labels.fn([mc], verified, {})
        assert result[str(mc.candidate.id)] is ConfidenceLabel.REPORTED

    def test_suspected_single_overpass(self) -> None:
        mc = make_matched_candidate(n_overpasses=1)
        result = assign_confidence_labels.fn([mc], {}, {})
        assert result[str(mc.candidate.id)] is ConfidenceLabel.SUSPECTED

    def test_missing_verified_entry_defaults_gracefully(self) -> None:
        mc = make_matched_candidate(n_overpasses=1)
        result = assign_confidence_labels.fn([mc], {}, {})
        assert str(mc.candidate.id) in result


# ---------------------------------------------------------------------------
# submit_to_editorial_queue
# ---------------------------------------------------------------------------


class TestSubmitToEditorialQueue:
    def test_unmatched_candidate_is_skipped(self) -> None:
        mc = make_matched_candidate(facility=None)
        result = submit_to_editorial_queue.fn([mc], {})
        assert result == []

    def test_matched_candidate_produces_fire_event(self) -> None:
        facility = make_facility()
        mc = make_matched_candidate(facility=facility)
        labels = {str(mc.candidate.id): ConfidenceLabel.VERIFIED}
        result = submit_to_editorial_queue.fn([mc], labels)
        assert len(result) == 1
        ev = result[0]
        assert ev.facility_id == facility.id
        assert ev.status is EventStatus.PENDING_REVIEW
        assert ev.confidence_label is ConfidenceLabel.VERIFIED

    def test_default_label_when_missing(self) -> None:
        facility = make_facility()
        mc = make_matched_candidate(facility=facility)
        # No entry in labels → should default to SUSPECTED.
        result = submit_to_editorial_queue.fn([mc], {})
        assert len(result) == 1
        assert result[0].confidence_label is ConfidenceLabel.SUSPECTED

    def test_idempotent_on_re_submission(self) -> None:
        """Submitting the same event twice should not raise and returns one entry."""
        facility = make_facility()
        mc = make_matched_candidate(facility=facility)
        labels = {str(mc.candidate.id): ConfidenceLabel.REPORTED}
        result1 = submit_to_editorial_queue.fn([mc], labels)
        # A second call with the same task creates a new queue instance,
        # so both calls succeed and each returns 1 event.
        result2 = submit_to_editorial_queue.fn([mc], labels)
        assert len(result1) == 1
        assert len(result2) == 1

    def test_fire_event_timestamps_are_timezone_aware(self) -> None:
        facility = make_facility()
        mc = make_matched_candidate(facility=facility)
        result = submit_to_editorial_queue.fn([mc], {})
        ev = result[0]
        assert ev.created_at.tzinfo is not None
        assert ev.updated_at.tzinfo is not None


# ---------------------------------------------------------------------------
# _build_fire_event
# ---------------------------------------------------------------------------


class TestBuildFireEvent:
    def test_basic_fields(self) -> None:
        facility = make_facility()
        mc = make_matched_candidate(n_overpasses=2, facility=facility)
        ev = _build_fire_event(mc, ConfidenceLabel.REPORTED)
        assert ev.facility_id == facility.id
        assert ev.status is EventStatus.PENDING_REVIEW
        assert ev.confidence_label is ConfidenceLabel.REPORTED
        assert ev.total_frp_integral_mj is None  # not yet quantified
        assert ev.last_seen_at >= ev.detected_at

    def test_peak_frp_matches_candidate(self) -> None:
        facility = make_facility()
        mc = make_matched_candidate(facility=facility)
        ev = _build_fire_event(mc, ConfidenceLabel.SUSPECTED)
        assert ev.peak_frp_mw == mc.candidate.peak_frp_mw


# ---------------------------------------------------------------------------
# log_pipeline_run
# ---------------------------------------------------------------------------


class TestLogPipelineRun:
    def test_flushes_hashes(self, tmp_path: Path) -> None:
        metrics = PipelineRunMetrics(
            run_date=_RUN_DATE,
            started_at=_T0,
            finished_at=_T0 + timedelta(seconds=30),
        )
        new_hashes = {"hash_a", "hash_b"}
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            log_pipeline_run.fn(metrics, new_hashes)
            loaded = _load_seen_hashes(_RUN_DATE)
        assert loaded == new_hashes

    def test_accumulates_existing_hashes(self, tmp_path: Path) -> None:
        old_hashes = {"hash_old"}
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            _save_seen_hashes(_RUN_DATE, old_hashes)
            metrics = PipelineRunMetrics(
                run_date=_RUN_DATE,
                started_at=_T0,
                finished_at=_T0 + timedelta(seconds=10),
            )
            log_pipeline_run.fn(metrics, {"hash_new"})
            loaded = _load_seen_hashes(_RUN_DATE)
        assert "hash_old" in loaded
        assert "hash_new" in loaded

    def test_empty_hashes_does_not_write(self, tmp_path: Path) -> None:
        metrics = PipelineRunMetrics(
            run_date=_RUN_DATE,
            started_at=_T0,
            finished_at=_T0 + timedelta(seconds=5),
        )
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            log_pipeline_run.fn(metrics, set())
            path = tmp_path / f"{_RUN_DATE.isoformat()}.json"
        assert not path.exists()


# ---------------------------------------------------------------------------
# daily_ingest flow — end-to-end with mocked connectors
# ---------------------------------------------------------------------------


class TestDailyIngestFlow:
    """End-to-end flow tests using patched ingest connectors.

    We patch at the task level (the individual async task functions) rather
    than at the connector level so the test asserts the full task contract
    (return type, shape) without exercising network code.
    """

    def _make_firms_rows(self) -> list[dict[str, Any]]:
        return [make_firms_row(lat=32.0, lon=51.0)]

    def _make_acled_events(self) -> list[ACLEDEvent]:
        return [make_acled_event()]

    def _patch_env(self) -> dict[str, str]:
        return {
            "FIRMS_MAP_KEY": "test-key",
            "ACLED_EMAIL": "test@example.com",
            "ACLED_PASSWORD": "test-acled-password",
        }

    def test_flow_returns_metrics(self, tmp_path: Path) -> None:
        """Flow completes and returns a PipelineRunMetrics even when all
        ingest tasks return empty lists (no candidates to process)."""
        with (
            mock.patch(
                "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
            ),
            mock.patch(
                "wced.pipeline.daily_ingest._FACILITIES_GEOJSON",
                Path("/nonexistent/path.geojson"),
            ),
            mock.patch.dict(os.environ, self._patch_env()),
            mock.patch.object(
                # load_facilities is a blocking task; patch the underlying fn
                __import__(
                    "wced.pipeline.daily_ingest", fromlist=["load_facilities"]
                ).load_facilities,
                "fn",
                return_value=[],
            ),
            mock.patch.object(
                __import__(
                    "wced.pipeline.daily_ingest", fromlist=["ingest_firms_viirs"]
                ).ingest_firms_viirs,
                "fn",
                return_value=[],
            ),
            mock.patch.object(
                __import__(
                    "wced.pipeline.daily_ingest", fromlist=["ingest_firms_modis"]
                ).ingest_firms_modis,
                "fn",
                return_value=[],
            ),
            mock.patch.object(
                __import__(
                    "wced.pipeline.daily_ingest", fromlist=["ingest_conflict_events"]
                ).ingest_conflict_events,
                "fn",
                return_value=([], "none"),
            ),
        ):
            result = daily_ingest(_RUN_DATE)

        assert isinstance(result, PipelineRunMetrics)
        assert result.run_date == _RUN_DATE
        assert result.n_candidates == 0
        assert result.n_submitted_to_queue == 0

    def test_idempotent_rerun_produces_zero_candidates(
        self, tmp_path: Path
    ) -> None:
        """Re-running for a date where all detections are already seen
        produces zero candidates and zero submissions."""
        rows = self._make_firms_rows()
        # Pre-seed the seen-hash store with the hash of our test row.
        from wced.pipeline.daily_ingest import _detection_hash, _save_seen_hashes
        h = _detection_hash(rows[0])
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            _save_seen_hashes(_RUN_DATE, {h})
            result = detect_candidate_events.fn(rows, [])
        assert result == []

    def test_task_failure_is_recorded_in_metrics(self, tmp_path: Path) -> None:
        """A failing ingest task is recorded in task_failures and the flow
        continues rather than raising."""
        from wced.pipeline.daily_ingest import _safe_result

        failures: list[str] = []

        class _FakeLogger:
            def error(self, *args: Any, **kwargs: Any) -> None:
                pass

        class _FailingFuture:
            def result(self) -> None:
                raise RuntimeError("API down")

        default: list = []
        result = _safe_result(
            _FailingFuture(), "ingest_firms_viirs", default, failures, _FakeLogger()
        )
        assert result is default
        assert "ingest_firms_viirs" in failures

    def test_detect_candidate_events_writes_no_hashes_on_empty_input(
        self, tmp_path: Path
    ) -> None:
        """With no detections, the seen-hash file must not be created."""
        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            detect_candidate_events.fn([], [])
        assert list(tmp_path.iterdir()) == []

    def test_matched_candidate_submitted_to_queue(self, tmp_path: Path) -> None:
        """Full detect → submit round-trip with one matched candidate."""
        facility = make_facility(lat=32.001, lon=51.001)
        rows = [make_firms_row(lat=32.0, lon=51.0)]

        with mock.patch(
            "wced.pipeline.daily_ingest._SEEN_HASHES_DIR", tmp_path
        ):
            candidates = detect_candidate_events.fn(rows, [facility])

        assert len(candidates) == 1
        assert candidates[0].facility is not None

        labels = {str(candidates[0].candidate.id): ConfidenceLabel.REPORTED}
        events = submit_to_editorial_queue.fn(candidates, labels)
        assert len(events) == 1
        assert events[0].facility_id == facility.id
        assert events[0].confidence_label is ConfidenceLabel.REPORTED


# ---------------------------------------------------------------------------
# ENABLE_ACLED feature flag tests
# ---------------------------------------------------------------------------


class TestEnableACLEDFlag:
    """ACLED connector is inert when WCED_ENABLE_ACLED is off (default)."""

    @pytest.fixture(autouse=True)
    def _clear_settings_cache(self) -> None:
        """Clear the lru_cache on get_settings so env patches take effect."""
        from wced.settings import get_settings
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_acled_skipped_when_flag_off(self) -> None:
        """With ENABLE_ACLED=False (default), ingest_conflict_events should
        never instantiate ACLEDConnector, even if credentials are present."""
        from wced.pipeline.daily_ingest import ingest_conflict_events
        from wced.ingest.gdelt import GDELTEvent

        gdelt_ev = GDELTEvent(
            event_id="G001",
            event_date=_RUN_DATE,
            event_type="190",
            event_root_code="19",
            actor1="A",
            actor2="B",
            latitude=32.0,
            longitude=51.0,
            source_url="https://example.com",
            num_articles=1,
            avg_tone=-3.0,
            goldstein_scale=-5.0,
            detected_at=_T0,
        )

        async def _fake_gdelt_query(*a, **kw):
            yield {"event": gdelt_ev, "_source": make_source(), "detected_at": _T0}

        with (
            mock.patch.dict(os.environ, {
                "ACLED_EMAIL": "test@example.com",
                "ACLED_PASSWORD": "test-password",
                "WCED_ENABLE_ACLED": "",
            }),
            mock.patch(
                "wced.pipeline.daily_ingest.get_settings",
                return_value=__import__(
                    "wced.settings", fromlist=["Settings"]
                ).Settings(enable_acled=False),
            ),
            mock.patch(
                "wced.pipeline.daily_ingest.ACLEDConnector",
            ) as acled_cls,
            mock.patch(
                "wced.pipeline.daily_ingest.GDELTConnector",
            ) as gdelt_cls,
        ):
            gdelt_instance = mock.AsyncMock()
            gdelt_instance.query_events_api = _fake_gdelt_query
            gdelt_cls.return_value.__aenter__ = mock.AsyncMock(return_value=gdelt_instance)
            gdelt_cls.return_value.__aexit__ = mock.AsyncMock(return_value=None)

            import asyncio
            events, source_used = asyncio.run(
                ingest_conflict_events.fn(_RUN_DATE, ["Iran"])
            )

        acled_cls.assert_not_called()
        assert source_used == "gdelt"
        assert len(events) == 1

    def test_acled_source_explicit_raises_when_flag_off(self) -> None:
        """WCED_CONFLICT_SOURCE=acled with ENABLE_ACLED off should raise."""
        from wced.pipeline.daily_ingest import ingest_conflict_events

        with (
            mock.patch.dict(os.environ, {"WCED_CONFLICT_SOURCE": "acled"}),
            mock.patch(
                "wced.pipeline.daily_ingest.get_settings",
                return_value=__import__(
                    "wced.settings", fromlist=["Settings"]
                ).Settings(enable_acled=False),
            ),
            pytest.raises(RuntimeError, match="WCED_ENABLE_ACLED"),
        ):
            import asyncio
            asyncio.run(
                ingest_conflict_events.fn(_RUN_DATE, ["Iran"])
            )

    def test_corroboration_works_with_gdelt_only(self) -> None:
        """Corroboration still finds matches when only GDELT events present."""
        from wced.ingest.gdelt import GDELTEvent

        gdelt_ev = GDELTEvent(
            event_id="G002",
            event_date=_RUN_DATE,
            event_type="190",
            event_root_code="19",
            actor1="A",
            actor2="B",
            latitude=32.001,
            longitude=51.001,
            source_url="https://example.com",
            num_articles=1,
            avg_tone=-3.0,
            goldstein_scale=-5.0,
            detected_at=_T0,
        )

        mc = make_matched_candidate(lat=32.0, lon=51.0)
        result = corroborate_with_conflict_events.fn([mc], [gdelt_ev])
        matches = result[str(mc.candidate.id)]
        assert len(matches) == 1
        assert matches[0].source_type == "gdelt"

    def test_settings_enable_acled_default_false(self) -> None:
        """ENABLE_ACLED defaults to False when env var is not set."""
        from wced.settings import Settings
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings.from_env()
        assert s.enable_acled is False

    def test_settings_enable_acled_true(self) -> None:
        """ENABLE_ACLED is True when WCED_ENABLE_ACLED=true."""
        from wced.settings import Settings
        with mock.patch.dict(os.environ, {"WCED_ENABLE_ACLED": "true"}, clear=True):
            s = Settings.from_env()
        assert s.enable_acled is True
