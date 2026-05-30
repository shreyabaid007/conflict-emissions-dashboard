"""Tests for wced.db.repositories.facility.

Covers:
- Round-trip from the canonical bootstrap GeoJSON into Facility objects.
- Schema-validation gate (malformed feature → ValidationError before insert).
- Country-filtered iteration.
- ``upsert`` idempotence on UUID.
- ``PostgisFacilityRepository`` raises NotImplementedError consistently.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema.exceptions import ValidationError

from wced.db.repositories.facility import (
    DEFAULT_GEOJSON_PATH,
    DEFAULT_SCHEMA_PATH,
    InMemoryFacilityRepository,
    PostgisFacilityRepository,
    parse_geojson,
)
from wced.models.facility import FacilityType


# ---------------------------------------------------------------------------
# canonical bootstrap file
# ---------------------------------------------------------------------------


class TestCanonicalBootstrap:
    def test_default_files_exist(self) -> None:
        assert DEFAULT_GEOJSON_PATH.exists()
        assert DEFAULT_SCHEMA_PATH.exists()

    def test_canonical_geojson_validates(self) -> None:
        # parse_geojson raises if the file is out of sync with the schema.
        facilities = parse_geojson(DEFAULT_GEOJSON_PATH)
        assert len(facilities) >= 13

    def test_priority_targets_present(self) -> None:
        names = {f.name for f in parse_geojson(DEFAULT_GEOJSON_PATH)}
        # Every target the user listed must be loadable.
        required = {
            "Tehran Refinery (Shahr-e Rey)",
            "Shahran fuel depot",
            "Aghdasieh fuel depot",
            "Fardis (Karaj) fuel depot",
            "Bandar Abbas Refinery",
            "Bandar Abbas naval base",
            "Lavan Island Refinery",
            "Kharg Island crude export terminal",
            "Haifa Refineries (Bazan)",
            "Ashdod Refinery (Paz)",
            "BAPCO Sitra Refinery",
            "Ras Laffan Industrial City",
        }
        missing = required - names
        assert not missing, f"missing seed facilities: {missing}"

    def test_country_coverage(self) -> None:
        facilities = parse_geojson(DEFAULT_GEOJSON_PATH)
        countries = {f.country for f in facilities}
        assert {"IRN", "ISR", "BHR", "QAT"} <= countries

    def test_every_facility_has_source_url(self) -> None:
        for f in parse_geojson(DEFAULT_GEOJSON_PATH):
            assert f.source_url, f"{f.name!r} missing source_url"


# ---------------------------------------------------------------------------
# schema validation gate
# ---------------------------------------------------------------------------


class TestSchemaGate:
    def test_invalid_geojson_rejected(self, tmp_path: Path) -> None:
        bad = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0, 0]},
                "properties": {
                    "name": "Bad",
                    "facility_type": "NOT_A_REAL_TYPE",  # ← violates enum
                    "country": "XXX",
                    "capacity_barrels": None,
                    "source_url": "https://example.com",
                    "added_at": "2026-05-23T00:00:00Z",
                    "added_by": "test",
                },
            }],
        }
        path = tmp_path / "bad.geojson"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValidationError):
            parse_geojson(path)

    def test_schema_optional_when_explicitly_disabled(self, tmp_path: Path) -> None:
        # When callers pass schema_path=None, malformed property data still
        # fails at Pydantic — never silently — but the JSON-schema check is
        # skipped. We assert it bypasses the schema check by sending a payload
        # that the schema would reject but Pydantic accepts (extra property
        # tolerated by Facility constructor since unknown keys are ignored
        # via property-side .get()).
        payload = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "id": "11111111-1111-1111-1111-111111111111",
                "geometry": {"type": "Point", "coordinates": [51.4, 35.6]},
                "properties": {
                    "name": "X",
                    "facility_type": "REFINERY",
                    "country": "IRN",
                    "capacity_barrels": None,
                    "source_url": "https://example.com",
                    "added_at": "2026-05-23T00:00:00Z",
                    "added_by": "test",
                    "extra_unknown_field": "would fail strict schema",
                },
            }],
        }
        path = tmp_path / "x.geojson"
        path.write_text(json.dumps(payload), encoding="utf-8")
        facilities = parse_geojson(path, schema_path=None)
        assert facilities[0].name == "X"


# ---------------------------------------------------------------------------
# in-memory repository
# ---------------------------------------------------------------------------


class TestInMemoryRepository:
    def test_load_geojson_counts(self) -> None:
        repo = InMemoryFacilityRepository()
        n = repo.load_geojson()
        assert n == len(repo)
        assert n >= 13

    def test_country_filter(self) -> None:
        repo = InMemoryFacilityRepository()
        repo.load_geojson()
        irn = list(repo.iter_by_country("IRN"))
        isr = list(repo.iter_by_country("ISR"))
        assert {f.name for f in isr} == {"Haifa Refineries (Bazan)", "Ashdod Refinery (Paz)"}
        # Iran has the bulk of seed coverage.
        assert len(irn) >= 7
        # Filter must not leak into the wrong country.
        for f in irn:
            assert f.country == "IRN"

    def test_get_roundtrip(self) -> None:
        repo = InMemoryFacilityRepository()
        repo.load_geojson()
        any_facility = next(iter(repo.iter_by_country("IRN")))
        fetched = repo.get(any_facility.id)
        assert fetched.id == any_facility.id
        assert fetched.name == any_facility.name

    def test_get_missing_raises(self) -> None:
        repo = InMemoryFacilityRepository()
        with pytest.raises(KeyError, match="Facility not found"):
            repo.get(UUID("00000000-0000-0000-0000-000000000000"))

    def test_upsert_is_idempotent_on_id(self) -> None:
        repo = InMemoryFacilityRepository()
        repo.load_geojson()
        before = len(repo)
        # Reload the same file — same UUIDs → row count unchanged.
        repo.load_geojson()
        assert len(repo) == before

    def test_refinery_has_polygon_geometry(self) -> None:
        repo = InMemoryFacilityRepository()
        repo.load_geojson()
        for f in repo.iter_by_country("ISR"):
            geom = f.geometry()
            assert geom.geom_type == "Polygon", f"{f.name} should be a polygon"
            assert geom.is_valid

    def test_capacity_units_documented_as_barrels(self) -> None:
        repo = InMemoryFacilityRepository()
        repo.load_geojson()
        # Spot-check a refinery whose throughput is widely published.
        haifa = next(f for f in repo.iter_by_country("ISR") if "Haifa" in f.name)
        assert haifa.capacity_barrels == pytest.approx(197_000)
        assert haifa.facility_type is FacilityType.REFINERY


# ---------------------------------------------------------------------------
# PostGIS stub
# ---------------------------------------------------------------------------


class TestPostgisStub:
    def test_upsert_delegates_to_session(self) -> None:
        from unittest.mock import MagicMock
        from datetime import UTC, datetime

        mock_session = MagicMock()
        repo = PostgisFacilityRepository(session=mock_session)
        from wced.models.facility import Facility

        f = Facility(
            name="Test",
            facility_type=FacilityType.REFINERY,
            geometry_wkt="POINT(51.4 35.6)",
            country="IRN",
            source_url="https://example.com",
            added_at=datetime.now(UTC),
        )
        result = repo.upsert(f)
        assert result == f.id
        mock_session.execute.assert_called_once()
        mock_session.flush.assert_called_once()
