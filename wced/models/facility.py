"""Facility registry data models.

A Facility is a piece of oil/fuel infrastructure that could plausibly emit
CO2 when burning: refineries, depots, petrochemical complexes, gas processing
plants, offshore platforms, tanker terminals, storage tank farms. Facilities
are long-lived metadata; FireEvents are observations attached to them.

Coordinates are stored as WKT (well-known text) so they round-trip through
PostGIS without DB-specific dependencies. Methodology reference:
methodology/v1.0.pdf §2 — "Facility Registry and Capacity Uncertainty".
"""
from __future__ import annotations

import enum
import re
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)
from shapely import wkt as shapely_wkt
from shapely.errors import GEOSException
from shapely.geometry.base import BaseGeometry


class FacilityType(str, enum.Enum):
    """Classification of oil/fuel infrastructure tracked by WCED v1.

    The set is intentionally narrow: V1 only covers infrastructure whose
    combustion is governed by the emission factors in
    ``data/emission_factors.yaml``. Adding a new type requires a paired
    emission factor entry and a methodology amendment.
    """

    REFINERY = "REFINERY"
    OIL_DEPOT = "OIL_DEPOT"
    PETROCHEMICAL = "PETROCHEMICAL"
    GAS_PROCESSING = "GAS_PROCESSING"
    OFFSHORE_PLATFORM = "OFFSHORE_PLATFORM"
    TANKER_TERMINAL = "TANKER_TERMINAL"
    STORAGE_TANK_FARM = "STORAGE_TANK_FARM"


# Accepted geometry types for facility footprints. A facility is either a
# single point (centroid) or a polygon (footprint). MultiPoint / MultiPolygon
# are deliberately excluded so each Facility row maps to exactly one place.
_ALLOWED_GEOM_TYPES = frozenset({"Point", "Polygon"})

# ISO 3166-1 alpha-3 country codes: three uppercase ASCII letters.
_ISO3_RE = re.compile(r"^[A-Z]{3}$")


class Facility(BaseModel):
    """A registered piece of oil/fuel infrastructure.

    Parameters
    ----------
    id : UUID
        Stable identifier. Pre-generated so re-importing the registry
        produces the same IDs.
    name : str
        Human-readable name as it appears in the source registry, e.g.
        "Abadan Refinery". Not unique on its own.
    facility_type : FacilityType
        One of the V1 categories. See ``FacilityType`` for the closed set.
    geometry_wkt : str
        Footprint as WKT. Must parse to a Shapely Point or Polygon in
        WGS84 (EPSG:4326). Validated on construction.
    country : str
        ISO 3166-1 alpha-3 code (e.g. "IRN", "ISR", "USA"). Uppercase.
    capacity_barrels : float or None
        Throughput or storage capacity in barrels. None when the source
        registry doesn't publish a figure; downstream code must handle
        the missing case explicitly rather than substituting a default.
    capacity_uncertainty_pct : float
        One-sigma symmetric uncertainty on capacity_barrels as a
        percentage (0 = perfectly known, 100 = ±100%). Default 30.0
        reflects the typical spread between published OSINT registries.
    operator : str or None
        Operating company at time of registration, when known.
    source_url : str
        URL of the registry entry or report we sourced this Facility from.
        Required — every facility must be auditable to its origin.
    added_at : datetime
        UTC timestamp when this Facility entered our registry.
    notes : str or None
        Free-text annotation: ambiguity in coordinates, partial closures,
        merged operators, etc.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1)
    facility_type: FacilityType
    geometry_wkt: str
    country: str
    capacity_barrels: float | None = Field(default=None, ge=0.0)
    capacity_uncertainty_pct: float = Field(default=30.0, ge=0.0, le=100.0)
    operator: str | None = None
    source_url: str = Field(min_length=1)
    added_at: AwareDatetime
    notes: str | None = None

    @field_validator("country")
    @classmethod
    def _validate_country_code(cls, value: str) -> str:
        if not _ISO3_RE.match(value):
            raise ValueError(
                f"country must be an ISO 3166-1 alpha-3 code (3 uppercase "
                f"letters), got {value!r}"
            )
        return value

    @field_validator("geometry_wkt")
    @classmethod
    def _validate_geometry_wkt(cls, value: str) -> str:
        try:
            geom: BaseGeometry = shapely_wkt.loads(value)
        except (GEOSException, ValueError, TypeError) as exc:
            raise ValueError(f"geometry_wkt is not parseable as WKT: {exc}") from exc

        if geom.is_empty:
            raise ValueError("geometry_wkt parses to an empty geometry")

        if geom.geom_type not in _ALLOWED_GEOM_TYPES:
            raise ValueError(
                f"geometry_wkt must be a Point or Polygon, got {geom.geom_type}"
            )

        # Self-intersecting polygons round-trip through WKT but are unusable
        # for spatial joins. Reject them at the boundary.
        if not geom.is_valid:
            raise ValueError(
                f"geometry_wkt parses to an invalid {geom.geom_type} geometry"
            )

        return value

    def geometry(self) -> BaseGeometry:
        """Return the parsed Shapely geometry. Always succeeds after validation."""
        return shapely_wkt.loads(self.geometry_wkt)
