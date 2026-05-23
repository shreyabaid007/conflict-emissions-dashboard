"""Tests for wced.verify.sentinel2_check."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import numpy as np
import pytest
import xarray as xr

from wced.ai.classify import FireClassification, FireLabel
from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection
from wced.ingest.sentinel2 import Sentinel2Error
from wced.models.event import DetectionSource
from wced.models.facility import Facility, FacilityType
from wced.models.provenance import Source, SourceType
from wced.provenance.store import InMemoryProvenanceStore
from wced.verify.sentinel2_check import VerificationStatus, verify_candidate


def _candidate() -> CandidateFireEvent:
    src = uuid4()
    h = FIRMSDetection(
        latitude=32.66,
        longitude=51.68,
        frp_mw=80.0,
        detected_at=datetime(2026, 3, 7, 9, 0, tzinfo=UTC),
        detection_source=DetectionSource.FIRMS_VIIRS,
        brightness_k=380.0,
        confidence="h",
        source_id=src,
    )
    return CandidateFireEvent(
        hotspots=(h,),
        centroid_lat=32.66,
        centroid_lon=51.68,
        first_detected_at=h.detected_at,
        last_detected_at=h.detected_at,
        peak_frp_mw=80.0,
        mean_frp_mw=80.0,
        n_overpasses=1,
        provenance_id=uuid4(),
    )


def _facility() -> Facility:
    return Facility(
        name="Isfahan Refinery",
        facility_type=FacilityType.REFINERY,
        geometry_wkt="POINT(51.68 32.66)",
        country="IRN",
        source_url="https://example.org/registry/2",
        added_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _dummy_chip() -> xr.Dataset:
    coords = {"y": np.linspace(32.65, 32.67, 4), "x": np.linspace(51.67, 51.69, 4)}
    arr = np.full((4, 4), 0.9, dtype="float32")
    return xr.Dataset(
        {
            "B04": (("y", "x"), np.full((4, 4), 0.1, dtype="float32")),
            "B03": (("y", "x"), np.full((4, 4), 0.1, dtype="float32")),
            "B02": (("y", "x"), np.full((4, 4), 0.1, dtype="float32")),
            "B12": (("y", "x"), arr),
        },
        coords=coords,
    )


def _source() -> Source:
    return Source(
        source_type=SourceType.SATELLITE,
        identifier="https://example.org/s2/abc",
        retrieved_at=datetime(2026, 3, 7, 12, 0, tzinfo=UTC),
        retrieved_by="wced.ingest.sentinel2",
        content_hash="a" * 64,
        metadata={"cloud_cover": 5.0},
    )


class TestNoScenes:
    def test_returns_awaiting_when_no_s2_items(self) -> None:
        store = InMemoryProvenanceStore()
        connector = MagicMock()
        connector.search_around.return_value = []

        result = verify_candidate(
            _candidate(),
            _facility(),
            store=store,
            s2_connector=connector,
        )

        assert result.status is VerificationStatus.AWAITING_OPTICAL_CHECK
        assert result.classification is None
        connector.fetch_chip.assert_not_called()


class TestFetchFailure:
    def test_returns_awaiting_when_chip_fetch_raises(self) -> None:
        store = InMemoryProvenanceStore()
        connector = MagicMock()
        item = MagicMock()
        item.id = "S2A_TILE_X"
        item.properties = {"eo:cloud_cover": 15.0}
        connector.search_around.return_value = [item]
        connector.fetch_chip.side_effect = Sentinel2Error("network down")

        result = verify_candidate(
            _candidate(),
            _facility(),
            store=store,
            s2_connector=connector,
        )

        assert result.status is VerificationStatus.AWAITING_OPTICAL_CHECK
        assert result.s2_item_id == "S2A_TILE_X"
        assert result.s2_cloud_cover == pytest.approx(15.0)
        assert "network down" in (result.notes or "")


class TestSuccessPath:
    def test_confirmed_fire_yields_verified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = InMemoryProvenanceStore()
        connector = MagicMock()
        item = MagicMock()
        item.id = "S2A_TILE_Y"
        item.properties = {"eo:cloud_cover": 3.0}
        connector.search_around.return_value = [item]
        connector.fetch_chip.return_value = (_dummy_chip(), _source())

        # Stub the classifier so the test does not depend on heuristic tuning.
        fake_pid = uuid4()
        classification = FireClassification(
            label=FireLabel.CONFIRMED_FIRE,
            confidence=0.92,
            rationale="ok",
            provenance_id=fake_pid,
        )
        monkeypatch.setattr(
            "wced.verify.sentinel2_check.classify_fire",
            lambda *a, **kw: classification,
        )

        result = verify_candidate(
            _candidate(),
            _facility(),
            store=store,
            s2_connector=connector,
        )

        assert result.status is VerificationStatus.VERIFIED
        assert result.classification is classification
        assert result.s2_item_id == "S2A_TILE_Y"
        # The S2 Source was recorded in the store.
        assert fake_pid in result.provenance_ids

    @pytest.mark.parametrize(
        "label,expected",
        [
            (FireLabel.GAS_FLARING, VerificationStatus.REJECTED),
            (FireLabel.FALSE_POSITIVE, VerificationStatus.REJECTED),
            (FireLabel.AMBIGUOUS, VerificationStatus.AMBIGUOUS),
        ],
    )
    def test_non_confirmed_labels_map_to_expected_status(
        self, monkeypatch: pytest.MonkeyPatch, label: FireLabel, expected: VerificationStatus
    ) -> None:
        store = InMemoryProvenanceStore()
        connector = MagicMock()
        item = MagicMock()
        item.id = "S2A_TILE_Z"
        item.properties = {"eo:cloud_cover": 8.0}
        connector.search_around.return_value = [item]
        connector.fetch_chip.return_value = (_dummy_chip(), _source())

        monkeypatch.setattr(
            "wced.verify.sentinel2_check.classify_fire",
            lambda *a, **kw: FireClassification(
                label=label, confidence=0.5, rationale="x", provenance_id=uuid4()
            ),
        )

        result = verify_candidate(
            _candidate(),
            _facility(),
            store=store,
            s2_connector=connector,
        )

        assert result.status is expected
