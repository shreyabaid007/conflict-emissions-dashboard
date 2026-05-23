"""Tests for wced.ai.classify with fixture chips for known incidents.

The three fixture chips cover the cases methodology v1.0 §4.3 requires the
classifier to distinguish:

  - Shahran depot fire on Mar 7 2026 (large saturated SWIR + RGB smoke)
  - Pars Refinery steady-state flaring (1–2 pixel SWIR point, no smoke)
  - Volcanic / wildfire hot spot in a chip whose facility is cold (false +ve)

The flaring chip is the only one routed through the AI path; the other two
are handled by the local heuristic alone.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import numpy as np
import pytest
import xarray as xr

from wced.ai.claude_client import AnthropicClient
from wced.ai.classify import (
    FireClassification,
    FireLabel,
    classify_fire,
)
from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection
from wced.models.event import DetectionSource
from wced.models.facility import Facility, FacilityType
from wced.provenance.store import InMemoryProvenanceStore
from wced.settings import Settings, _SecretStr


# ---------------------------------------------------------------------------
# Fixture chips and supporting objects
# ---------------------------------------------------------------------------


def _chip(red: np.ndarray, green: np.ndarray, blue: np.ndarray, swir: np.ndarray) -> xr.Dataset:
    coords = {"y": np.linspace(32.0, 32.05, red.shape[0]), "x": np.linspace(51.0, 51.05, red.shape[1])}
    return xr.Dataset(
        {
            "B04": (("y", "x"), red.astype("float32")),
            "B03": (("y", "x"), green.astype("float32")),
            "B02": (("y", "x"), blue.astype("float32")),
            "B12": (("y", "x"), swir.astype("float32")),
        },
        coords=coords,
    )


def _shahran_fire_chip() -> xr.Dataset:
    shape = (8, 8)
    red = np.full(shape, 0.10, dtype="float32")
    green = np.full(shape, 0.12, dtype="float32")
    blue = np.full(shape, 0.14, dtype="float32")
    swir = np.full(shape, 0.08, dtype="float32")
    # Strongly saturated SWIR core with smoke-elevated RGB nearby.
    swir[3:5, 3:5] = 0.95
    red[3:5, 3:5] = 0.20
    return _chip(red, green, blue, swir)


def _pars_flaring_chip() -> xr.Dataset:
    shape = (8, 8)
    red = np.full(shape, 0.12, dtype="float32")
    green = np.full(shape, 0.13, dtype="float32")
    blue = np.full(shape, 0.14, dtype="float32")
    swir = np.full(shape, 0.10, dtype="float32")
    # Single elevated SWIR pixel — below saturation, ambiguous to heuristic.
    swir[4, 4] = 0.30
    return _chip(red, green, blue, swir)


def _cold_chip() -> xr.Dataset:
    shape = (8, 8)
    red = np.full(shape, 0.18, dtype="float32")
    green = np.full(shape, 0.20, dtype="float32")
    blue = np.full(shape, 0.22, dtype="float32")
    swir = np.full(shape, 0.15, dtype="float32")
    return _chip(red, green, blue, swir)


def _candidate(lat: float = 32.025, lon: float = 51.025) -> CandidateFireEvent:
    src = uuid4()
    hotspot = FIRMSDetection(
        latitude=lat,
        longitude=lon,
        frp_mw=50.0,
        detected_at=datetime(2026, 3, 7, 9, 0, tzinfo=UTC),
        detection_source=DetectionSource.FIRMS_VIIRS,
        brightness_k=370.0,
        confidence="h",
        source_id=src,
    )
    return CandidateFireEvent(
        hotspots=(hotspot,),
        centroid_lat=lat,
        centroid_lon=lon,
        first_detected_at=hotspot.detected_at,
        last_detected_at=hotspot.detected_at,
        peak_frp_mw=50.0,
        mean_frp_mw=50.0,
        n_overpasses=1,
        provenance_id=uuid4(),
    )


def _facility(name: str = "Shahran Depot", ftype: FacilityType = FacilityType.OIL_DEPOT) -> Facility:
    return Facility(
        name=name,
        facility_type=ftype,
        geometry_wkt="POINT(51.025 32.025)",
        country="IRN",
        source_url="https://example.org/registry/1",
        added_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _ai_client(verdict_input: dict) -> AnthropicClient:
    """Build an AnthropicClient backed by a stub SDK that returns a tool call."""
    from types import SimpleNamespace

    sdk = MagicMock()
    sdk.messages.create.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="record_structured_output",
                input=verdict_input,
            )
        ],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )
    settings = Settings(anthropic_api_key=_SecretStr("test"))
    return AnthropicClient(settings=settings, client=sdk)


# ---------------------------------------------------------------------------
# Heuristic-only paths
# ---------------------------------------------------------------------------


class TestHeuristicPath:
    def test_shahran_fire_classified_confirmed_by_heuristic(self) -> None:
        store = InMemoryProvenanceStore()
        candidate = _candidate()
        facility = _facility()

        # Pass an explicit client that would explode if accidentally invoked.
        sentinel_client = MagicMock(spec=AnthropicClient)
        sentinel_client.call.side_effect = AssertionError("AI path should not run")

        result = classify_fire(
            _shahran_fire_chip(),
            candidate,
            facility,
            store=store,
            client=sentinel_client,
        )

        assert isinstance(result, FireClassification)
        assert result.label is FireLabel.CONFIRMED_FIRE
        assert result.confidence >= 0.85
        sentinel_client.call.assert_not_called()
        # Provenance record was written and references the candidate's record.
        rec = store.get(result.provenance_id)
        assert rec.produced_by == "wced.ai.classify"
        assert candidate.provenance_id in rec.inputs

    def test_cold_chip_classified_false_positive_by_heuristic(self) -> None:
        store = InMemoryProvenanceStore()
        candidate = _candidate()
        facility = _facility(ftype=FacilityType.REFINERY, name="Cold Facility")

        result = classify_fire(
            _cold_chip(),
            candidate,
            facility,
            store=store,
            client=MagicMock(spec=AnthropicClient),  # must not be called
        )

        assert result.label is FireLabel.FALSE_POSITIVE
        assert result.confidence >= 0.8


# ---------------------------------------------------------------------------
# AI escalation path
# ---------------------------------------------------------------------------


class TestAiPath:
    def test_pars_flaring_escalates_to_ai_and_returns_flaring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PNG rendering needs Pillow; the escalation logic does not. Stub it
        # so the test runs in environments without Pillow installed.
        monkeypatch.setattr(
            "wced.ai.classify._render_composite_png", lambda chip: b"\x89PNG\r\n\x1a\n"
        )
        store = InMemoryProvenanceStore()
        candidate = _candidate(lat=27.78, lon=52.36)
        facility = _facility(name="Pars Refinery", ftype=FacilityType.REFINERY)

        client = _ai_client(
            {
                "label": "GAS_FLARING",
                "confidence": 0.82,
                "rationale": "Single-pixel SWIR hot point at the known flare stack; "
                "no smoke plume; no damage to surrounding tanks.",
            }
        )

        result = classify_fire(
            _pars_flaring_chip(),
            candidate,
            facility,
            store=store,
            client=client,
        )

        assert result.label is FireLabel.GAS_FLARING
        assert result.confidence == pytest.approx(0.82)
        # The AI client was actually invoked, and its DERIVED Source was recorded.
        assert client.last_source is not None
        # Provenance chain includes the Claude Source.
        rec = store.get(result.provenance_id)
        assert client.last_source.id in rec.inputs
        # The recorded method is the versioned vision prompt.
        assert "vision_classify" in rec.method
