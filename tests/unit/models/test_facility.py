"""Tests for wced.models.facility.

Covers:
- Field defaults and constraints
- Country code validation (ISO 3166-1 alpha-3)
- WKT geometry validation: accepts Point and Polygon, rejects everything else
- Hypothesis property tests: random non-WKT strings must never validate;
  random valid Point WKT must always validate.
- Frozen-model behavior
"""
from __future__ import annotations

import string
from datetime import datetime, timezone
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from wced.models.facility import Facility, FacilityType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_facility(**kwargs: object) -> Facility:
    defaults: dict[str, object] = dict(
        name="Abadan Refinery",
        facility_type=FacilityType.REFINERY,
        geometry_wkt="POINT(48.3 30.36)",
        country="IRN",
        capacity_barrels=400_000.0,
        operator="NIORDC",
        source_url="https://example.org/registry/abadan",
        added_at=utcnow(),
    )
    return Facility(**(defaults | kwargs))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Construction and defaults
# ---------------------------------------------------------------------------


class TestFacilityConstruction:
    def test_minimum_required_fields(self) -> None:
        f = Facility(
            name="X",
            facility_type=FacilityType.OIL_DEPOT,
            geometry_wkt="POINT(0 0)",
            country="USA",
            source_url="https://example.org",
            added_at=utcnow(),
        )
        assert isinstance(f.id, UUID)
        assert f.capacity_barrels is None
        assert f.capacity_uncertainty_pct == 30.0
        assert f.operator is None
        assert f.notes is None

    def test_all_facility_types_accepted(self) -> None:
        for ft in FacilityType:
            f = make_facility(facility_type=ft)
            assert f.facility_type is ft

    def test_polygon_wkt_accepted(self) -> None:
        f = make_facility(geometry_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))")
        geom = f.geometry()
        assert geom.geom_type == "Polygon"

    def test_point_wkt_accepted(self) -> None:
        f = make_facility(geometry_wkt="POINT(48.3 30.36)")
        assert f.geometry().geom_type == "Point"

    def test_facility_is_frozen(self) -> None:
        f = make_facility()
        with pytest.raises(ValidationError):
            f.name = "Other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Field constraint validation
# ---------------------------------------------------------------------------


class TestFacilityFieldConstraints:
    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_facility(name="")

    def test_empty_source_url_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_facility(source_url="")

    def test_negative_capacity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_facility(capacity_barrels=-1.0)

    def test_uncertainty_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_facility(capacity_uncertainty_pct=-0.1)
        with pytest.raises(ValidationError):
            make_facility(capacity_uncertainty_pct=100.1)

    def test_added_at_must_be_aware(self) -> None:
        with pytest.raises(ValidationError):
            make_facility(added_at=datetime(2026, 3, 1))  # naive


class TestCountryCodeValidation:
    @pytest.mark.parametrize("code", ["IRN", "ISR", "USA", "GBR", "ARE"])
    def test_valid_iso3_accepted(self, code: str) -> None:
        f = make_facility(country=code)
        assert f.country == code

    @pytest.mark.parametrize(
        "code",
        [
            "ir",       # too short
            "IRAN",     # too long
            "irn",      # lowercase
            "IR1",      # digit
            "IR ",      # space
            "",         # empty
            "I R",      # space inside
        ],
    )
    def test_invalid_country_rejected(self, code: str) -> None:
        with pytest.raises(ValidationError):
            make_facility(country=code)


# ---------------------------------------------------------------------------
# WKT validation — explicit cases
# ---------------------------------------------------------------------------


class TestWKTValidation:
    @pytest.mark.parametrize(
        "wkt",
        [
            "POINT(0 0)",
            "POINT(48.3 30.36)",
            "POINT(-122.4 37.8)",
            "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
            "POLYGON((48 30, 49 30, 49 31, 48 31, 48 30))",
        ],
    )
    def test_valid_geometries_accepted(self, wkt: str) -> None:
        f = make_facility(geometry_wkt=wkt)
        assert f.geometry_wkt == wkt

    @pytest.mark.parametrize(
        "wkt",
        [
            "",                                         # empty
            "NOT WKT AT ALL",                           # garbage
            "POINT 1 2",                                # missing parens
            "POINT()",                                  # empty point
            "POLYGON((0 0))",                           # too few points
            "POLYGON EMPTY",                            # empty polygon
            "POINT EMPTY",                              # empty point form
            "GARBAGE(1 2)",                             # unknown type
            "POINT(1)",                                 # missing coord
        ],
    )
    def test_unparseable_wkt_rejected(self, wkt: str) -> None:
        with pytest.raises(ValidationError):
            make_facility(geometry_wkt=wkt)

    @pytest.mark.parametrize(
        "wkt",
        [
            "LINESTRING(0 0, 1 1)",
            "MULTIPOINT((0 0), (1 1))",
            "MULTIPOLYGON(((0 0, 1 0, 1 1, 0 0)))",
            "GEOMETRYCOLLECTION(POINT(0 0))",
        ],
    )
    def test_non_point_non_polygon_rejected(self, wkt: str) -> None:
        with pytest.raises(ValidationError) as excinfo:
            make_facility(geometry_wkt=wkt)
        assert "Point or Polygon" in str(excinfo.value)

    def test_self_intersecting_polygon_rejected(self) -> None:
        # Classic bowtie — parses fine but is_valid is False.
        with pytest.raises(ValidationError) as excinfo:
            make_facility(geometry_wkt="POLYGON((0 0, 1 1, 0 1, 1 0, 0 0))")
        assert "invalid" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# WKT validation — Hypothesis property tests
# ---------------------------------------------------------------------------

# Strategy: arbitrary unicode text. Effectively all of these are not valid WKT.
# Filter out the (vanishingly rare) chance that random text happens to parse.
_random_text = st.text(min_size=0, max_size=80).filter(
    lambda s: not s.upper().lstrip().startswith(("POINT", "POLYGON"))
)


class TestWKTPropertyBased:
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        deadline=None,
    )
    @given(garbage=_random_text)
    def test_arbitrary_text_is_rejected(self, garbage: str) -> None:
        """Random unicode strings must never be accepted as WKT."""
        with pytest.raises(ValidationError):
            make_facility(geometry_wkt=garbage)

    @settings(max_examples=200, deadline=None)
    @given(
        lon=st.floats(
            min_value=-180.0,
            max_value=180.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        lat=st.floats(
            min_value=-90.0,
            max_value=90.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_well_formed_point_always_accepted(self, lon: float, lat: float) -> None:
        """Any finite (lon, lat) within geographic bounds is a valid POINT."""
        wkt = f"POINT({lon} {lat})"
        f = make_facility(geometry_wkt=wkt)
        geom = f.geometry()
        assert geom.geom_type == "Point"
        assert geom.x == pytest.approx(lon)
        assert geom.y == pytest.approx(lat)

    @settings(max_examples=100, deadline=None)
    @given(
        keyword=st.sampled_from(
            ["LINESTRING", "MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON"]
        ),
    )
    def test_other_geometry_types_always_rejected(self, keyword: str) -> None:
        """Even syntactically-valid non-Point/Polygon WKT must be rejected."""
        # Pick a minimal valid body per type.
        bodies = {
            "LINESTRING": "(0 0, 1 1)",
            "MULTIPOINT": "((0 0), (1 1))",
            "MULTILINESTRING": "((0 0, 1 1), (2 2, 3 3))",
            "MULTIPOLYGON": "(((0 0, 1 0, 1 1, 0 1, 0 0)))",
        }
        with pytest.raises(ValidationError):
            make_facility(geometry_wkt=f"{keyword}{bodies[keyword]}")
