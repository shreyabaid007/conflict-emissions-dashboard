"""Tests for scripts/bootstrap_facilities.py.

Covers:
- The embedded seed covers every priority target named in the spec.
- ``build_collection`` produces output that validates against the schema.
- Re-running with the same inputs yields the same feature IDs (uuid5 determinism).
- GEM CSV parsing skips out-of-scope rows and respects country/type filters.
- ``merge_gem_into_seed`` deduplicates by gem_id.
- Overpass enrichment replaces Point geometries when the API returns a way,
  using a stubbed ``OverpassClient`` (no network).
"""
from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.bootstrap_facilities import (
    SEED,
    OverpassClient,
    SeedFacility,
    build_collection,
    enrich_with_overpass,
    facility_uuid,
    load_gem_rows,
    merge_gem_into_seed,
    validate,
)


# ---------------------------------------------------------------------------
# seed coverage
# ---------------------------------------------------------------------------


class TestSeed:
    def test_priority_targets_present(self) -> None:
        slugs = {s.slug for s in SEED}
        required = {
            "tehran-refinery-shahr-rey",
            "shahran-depot",
            "aghdasieh-depot",
            "fardis-karaj-depot",
            "bandar-abbas-refinery",
            "bandar-abbas-naval-base",
            "lavan-island-refinery",
            "kharg-island-terminal",
            "haifa-bazan-refinery",
            "ashdod-paz-refinery",
            "bapco-sitra-refinery",
            "ras-laffan-industrial-city",
        }
        assert required <= slugs

    def test_facility_uuid_is_deterministic(self) -> None:
        assert facility_uuid("tehran-refinery-shahr-rey") == facility_uuid(
            "tehran-refinery-shahr-rey"
        )
        # Different slug → different UUID.
        assert facility_uuid("a") != facility_uuid("b")


# ---------------------------------------------------------------------------
# build + validate
# ---------------------------------------------------------------------------


class TestBuildCollection:
    def test_collection_validates_against_schema(self) -> None:
        collection = build_collection(SEED, added_at=datetime(2026, 5, 23, tzinfo=UTC))
        validate(collection)  # raises on schema violation

    def test_feature_ids_match_uuid5(self) -> None:
        collection = build_collection(SEED, added_at=datetime(2026, 5, 23, tzinfo=UTC))
        # Pick any seed and check its derived feature ID.
        seed = next(s for s in SEED if s.slug == "bandar-abbas-refinery")
        ids = {f["id"] for f in collection["features"]}
        assert str(facility_uuid(seed.slug)) in ids

    def test_added_at_is_zulu_iso(self) -> None:
        ts = datetime(2026, 5, 23, 12, 34, 56, tzinfo=UTC)
        collection = build_collection(SEED[:1], added_at=ts)
        assert collection["features"][0]["properties"]["added_at"] == "2026-05-23T12:34:56Z"

    def test_added_by_is_generator(self) -> None:
        collection = build_collection(SEED[:1], added_at=datetime(2026, 1, 1, tzinfo=UTC))
        assert collection["features"][0]["properties"]["added_by"] == "scripts/bootstrap_facilities.py"


# ---------------------------------------------------------------------------
# GEM CSV merge
# ---------------------------------------------------------------------------


def _write_gem_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    headers = [
        "GEM unit/phase ID",
        "Unit name",
        "Country/Area",
        "Capacity (bbl/d)",
        "Latitude",
        "Longitude",
        "Operator",
        "Unit type",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


class TestGEM:
    def test_skips_out_of_scope_country(self, tmp_path: Path) -> None:
        path = _write_gem_csv(tmp_path / "gem.csv", [
            {
                "GEM unit/phase ID": "G1",
                "Unit name": "Outside refinery",
                "Country/Area": "Tuvalu",  # out of scope
                "Capacity (bbl/d)": "100000",
                "Latitude": "0", "Longitude": "0",
                "Operator": "X",
                "Unit type": "Crude oil refinery",
            },
            {
                "GEM unit/phase ID": "G2",
                "Unit name": "Iran refinery",
                "Country/Area": "Iran",
                "Capacity (bbl/d)": "200000",
                "Latitude": "32.0", "Longitude": "52.0",
                "Operator": "NIORDC",
                "Unit type": "Crude oil refinery",
            },
        ])
        rows = load_gem_rows(path)
        assert [r.gem_id for r in rows] == ["G2"]
        assert rows[0].country == "IRN"
        assert rows[0].facility_type == "REFINERY"
        assert rows[0].capacity_barrels == 200_000

    def test_skips_unknown_unit_type(self, tmp_path: Path) -> None:
        path = _write_gem_csv(tmp_path / "gem.csv", [
            {
                "GEM unit/phase ID": "G3",
                "Unit name": "Power plant",
                "Country/Area": "Iran",
                "Capacity (bbl/d)": "",
                "Latitude": "32.0", "Longitude": "52.0",
                "Operator": "X",
                "Unit type": "Coal-fired power station",
            },
        ])
        assert load_gem_rows(path) == []

    def test_skips_non_numeric_coords(self, tmp_path: Path) -> None:
        path = _write_gem_csv(tmp_path / "gem.csv", [
            {
                "GEM unit/phase ID": "G4",
                "Unit name": "Broken",
                "Country/Area": "Iran",
                "Capacity (bbl/d)": "100000",
                "Latitude": "n/a", "Longitude": "52.0",
                "Operator": "X",
                "Unit type": "Crude oil refinery",
            },
        ])
        assert load_gem_rows(path) == []

    def test_merge_dedups_by_gem_id(self) -> None:
        seed_with_gem = (
            SeedFacility(
                slug="manual-entry",
                name="Manual",
                facility_type="REFINERY",
                country="IRN",
                geometry={"type": "Point", "coordinates": [52.0, 32.0]},
                capacity_barrels=None,
                source_url="https://example.com",
                gem_id="G42",
            ),
        )
        gem_rows = [
            SeedFacility(
                slug="gem-G42",
                name="Duplicate",
                facility_type="REFINERY",
                country="IRN",
                geometry={"type": "Point", "coordinates": [52.0, 32.0]},
                capacity_barrels=None,
                source_url="https://gem",
                gem_id="G42",
            ),
            SeedFacility(
                slug="gem-G99",
                name="New row",
                facility_type="REFINERY",
                country="IRN",
                geometry={"type": "Point", "coordinates": [53.0, 33.0]},
                capacity_barrels=None,
                source_url="https://gem",
                gem_id="G99",
            ),
        ]
        merged = merge_gem_into_seed(seed_with_gem, gem_rows)
        assert [m.slug for m in merged] == ["manual-entry", "gem-G99"]


# ---------------------------------------------------------------------------
# Overpass enrichment (no network)
# ---------------------------------------------------------------------------


class _StubOverpass(OverpassClient):
    """OverpassClient stub that returns a canned closed way for any query."""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__()
        self._response = response

    def query(self, ql: str) -> dict[str, Any]:
        return self._response


class TestOverpass:
    def test_enriches_point_seeds(self) -> None:
        point_seed = SeedFacility(
            slug="depot-x",
            name="Depot X",
            facility_type="OIL_DEPOT",
            country="IRN",
            geometry={"type": "Point", "coordinates": [51.3, 35.8]},
            capacity_barrels=None,
            source_url="https://example.com",
        )
        polygon_seed = SeedFacility(
            slug="refinery-y",
            name="Refinery Y",
            facility_type="REFINERY",
            country="IRN",
            geometry={"type": "Polygon", "coordinates": [[
                [51.0, 35.0], [51.1, 35.0], [51.1, 35.1], [51.0, 35.1], [51.0, 35.0]
            ]]},
            capacity_barrels=100_000,
            source_url="https://example.com",
        )
        canned = {
            "elements": [{
                "type": "way",
                "id": 12345,
                "geometry": [
                    {"lon": 51.300, "lat": 35.800},
                    {"lon": 51.302, "lat": 35.800},
                    {"lon": 51.302, "lat": 35.802},
                    {"lon": 51.300, "lat": 35.802},
                ],
            }],
        }
        result = enrich_with_overpass([point_seed, polygon_seed], client=_StubOverpass(canned))
        assert result[0].geometry["type"] == "Polygon"
        assert result[0].osm_id == "way/12345"
        # Polygon seed must not be touched.
        assert result[1].geometry == polygon_seed.geometry
        assert result[1].osm_id is None

    def test_empty_response_keeps_point(self) -> None:
        seed = SeedFacility(
            slug="depot-empty",
            name="Empty",
            facility_type="OIL_DEPOT",
            country="IRN",
            geometry={"type": "Point", "coordinates": [51.0, 35.0]},
            capacity_barrels=None,
            source_url="https://example.com",
        )
        result = enrich_with_overpass([seed], client=_StubOverpass({"elements": []}))
        assert result[0].geometry == seed.geometry
