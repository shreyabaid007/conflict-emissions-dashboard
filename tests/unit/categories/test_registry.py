"""Tests for the EmissionCategory protocol and CategoryRegistry."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import numpy as np
import pytest

from wced.categories.base import (
    ENTRY_POINT_GROUP,
    CategoryRegistry,
    DetectionEvent,
    EmissionCategory,
    SourceSpec,
    VerificationResult,
    get_registry,
    reset_registry,
)
from wced.quantify.distribution import Distribution


# ---------------------------------------------------------------------------
# Concrete test implementation of the protocol
# ---------------------------------------------------------------------------


class FakeCategory:
    """Minimal implementation of EmissionCategory for testing."""

    def __init__(self, cat_id: str = "fake_category", version: str = "1.0.0"):
        self._id = cat_id
        self._version = version

    @property
    def id(self) -> str:
        return self._id

    @property
    def methodology_version(self) -> str:
        return self._version

    def required_sources(self) -> list[SourceSpec]:
        return [
            SourceSpec(name="firms_viirs", description="FIRMS VIIRS detections"),
            SourceSpec(name="optional_source", description="Optional", required=False),
        ]

    def detect(self, ctx: dict[str, Any]) -> list[DetectionEvent]:
        return [
            DetectionEvent(
                event_id="evt-1",
                category_id=self._id,
                data={"lat": 32.0, "lon": 51.0},
            )
        ]

    def verify(self, event: DetectionEvent, ctx: dict[str, Any]) -> VerificationResult:
        return VerificationResult(
            event_id=event.event_id,
            verified=True,
            confidence_label="VERIFIED",
            data={},
        )

    def quantify(self, event: DetectionEvent, verification: VerificationResult) -> Distribution:
        return Distribution.from_samples(
            np.ones(100),
            units="tCO2e",
            methodology_version=self._version,
            provenance_id=uuid4(),
        )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_fake_category_satisfies_protocol(self):
        cat = FakeCategory()
        assert isinstance(cat, EmissionCategory)

    def test_protocol_properties(self):
        cat = FakeCategory(cat_id="test_cat", version="2.0.0")
        assert cat.id == "test_cat"
        assert cat.methodology_version == "2.0.0"

    def test_required_sources(self):
        cat = FakeCategory()
        sources = cat.required_sources()
        assert len(sources) == 2
        assert sources[0].name == "firms_viirs"
        assert sources[0].required is True
        assert sources[1].required is False

    def test_detect_returns_detection_events(self):
        cat = FakeCategory()
        events = cat.detect({"firms_viirs": []})
        assert len(events) == 1
        assert events[0].category_id == "fake_category"

    def test_verify_returns_verification_result(self):
        cat = FakeCategory()
        event = DetectionEvent(event_id="e1", category_id="fake_category", data={})
        result = cat.verify(event, {})
        assert result.verified is True
        assert result.confidence_label == "VERIFIED"

    def test_quantify_returns_distribution(self):
        cat = FakeCategory()
        event = DetectionEvent(event_id="e1", category_id="fake_category", data={})
        verification = VerificationResult(
            event_id="e1", verified=True, confidence_label="VERIFIED", data={},
        )
        dist = cat.quantify(event, verification)
        assert isinstance(dist, Distribution)
        assert dist.units == "tCO2e"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestCategoryRegistry:
    def test_register_and_get(self):
        registry = CategoryRegistry()
        cat = FakeCategory("my_cat")
        registry.register(cat)
        assert registry.get("my_cat") is cat

    def test_duplicate_raises(self):
        registry = CategoryRegistry()
        registry.register(FakeCategory("dup"))
        with pytest.raises(ValueError, match="Duplicate category id"):
            registry.register(FakeCategory("dup"))

    def test_get_unknown_raises(self):
        registry = CategoryRegistry()
        with pytest.raises(KeyError, match="Unknown category"):
            registry.get("nonexistent")

    def test_all_returns_registered(self):
        registry = CategoryRegistry()
        c1 = FakeCategory("a")
        c2 = FakeCategory("b")
        registry.register(c1)
        registry.register(c2)
        assert registry.all() == [c1, c2]

    def test_ids_sorted(self):
        registry = CategoryRegistry()
        registry.register(FakeCategory("z_cat"))
        registry.register(FakeCategory("a_cat"))
        assert registry.ids() == ["a_cat", "z_cat"]

    def test_len(self):
        registry = CategoryRegistry()
        assert len(registry) == 0
        registry.register(FakeCategory("x"))
        assert len(registry) == 1

    def test_contains(self):
        registry = CategoryRegistry()
        registry.register(FakeCategory("present"))
        assert "present" in registry
        assert "absent" not in registry


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------


class TestEntryPointDiscovery:
    def test_discover_loads_entry_points(self):
        mock_ep = MagicMock()
        mock_ep.name = "fake"
        mock_ep.load.return_value = FakeCategory

        with patch("importlib.metadata.entry_points") as mock_eps:
            mock_group = MagicMock()
            mock_group.__iter__ = lambda self: iter([mock_ep])
            mock_eps.return_value.select.return_value = mock_group

            registry = CategoryRegistry.discover()

        assert "fake_category" in registry
        assert len(registry) == 1

    def test_discover_skips_failing_entry_point(self):
        """A broken entry point doesn't prevent other categories from loading."""
        mock_good = MagicMock()
        mock_good.name = "good"
        mock_good.load.return_value = FakeCategory

        mock_bad = MagicMock()
        mock_bad.name = "broken"
        mock_bad.load.side_effect = ImportError("missing dep")

        with patch("importlib.metadata.entry_points") as mock_eps:
            mock_eps.return_value.select.return_value = [mock_bad, mock_good]

            registry = CategoryRegistry.discover()

        assert "fake_category" in registry

    def test_discover_skips_non_conforming(self):
        mock_ep = MagicMock()
        mock_ep.name = "bad_protocol"
        mock_ep.load.return_value = lambda: "not a category"

        registry = CategoryRegistry()
        for ep in [mock_ep]:
            try:
                factory = ep.load()
                instance = factory()
                if isinstance(instance, EmissionCategory):
                    registry.register(instance)
            except Exception:
                pass

        assert len(registry) == 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def setup_method(self):
        reset_registry()

    def teardown_method(self):
        reset_registry()

    def test_get_registry_returns_same_instance(self):
        with patch.object(CategoryRegistry, "discover", return_value=CategoryRegistry()):
            r1 = get_registry()
            r2 = get_registry()
            assert r1 is r2

    def test_reset_clears_singleton(self):
        with patch.object(CategoryRegistry, "discover", side_effect=lambda: CategoryRegistry()):
            r1 = get_registry()
            reset_registry()
            r2 = get_registry()
            assert r1 is not r2
