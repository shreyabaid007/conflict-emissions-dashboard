"""Facility repository — in-memory and PostGIS-backed implementations.

Both implementations share one Protocol so callers can swap backends
without changing application code.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from jsonschema import Draft202012Validator
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from wced.db import models
from wced.models.facility import Facility, FacilityType

log = logging.getLogger(__name__)

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "data" / "facilities" / "facilities.schema.json"
DEFAULT_GEOJSON_PATH = Path(__file__).resolve().parents[3] / "data" / "facilities" / "iran_oil_gas.geojson"


def _feature_to_facility(feature: dict[str, Any]) -> Facility:
    """Map one validated GeoJSON Feature dict onto a ``Facility``."""
    feature_id = feature.get("id")
    if feature_id is None:
        raise ValueError(
            f"facility feature missing top-level 'id' "
            f"(name={feature['properties'].get('name')!r})"
        )
    geom: BaseGeometry = shape(feature["geometry"])
    props = feature["properties"]
    return Facility(
        id=UUID(str(feature_id)),
        name=props["name"],
        facility_type=FacilityType(props["facility_type"]),
        geometry_wkt=geom.wkt,
        country=props["country"],
        capacity_barrels=props.get("capacity_barrels"),
        capacity_uncertainty_pct=props.get("capacity_uncertainty_pct", 30.0),
        operator=props.get("operator"),
        source_url=props["source_url"],
        added_at=datetime.fromisoformat(props["added_at"].replace("Z", "+00:00")),
        notes=props.get("notes"),
    )


def parse_geojson(
    path: Path,
    *,
    schema_path: Path | None = DEFAULT_SCHEMA_PATH,
) -> list[Facility]:
    """Read a facility GeoJSON, schema-validate it, and return Facility rows."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if schema_path is not None:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(payload)
    return [_feature_to_facility(f) for f in payload["features"]]


@runtime_checkable
class FacilityRepository(Protocol):
    """Interface that every facility-registry backend must satisfy."""

    def upsert(self, facility: Facility) -> UUID: ...
    def load_geojson(self, path: Path = DEFAULT_GEOJSON_PATH, *, schema_path: Path | None = DEFAULT_SCHEMA_PATH) -> int: ...
    def get(self, facility_id: UUID) -> Facility: ...
    def iter_by_country(self, country_iso3: str) -> Iterator[Facility]: ...
    def __len__(self) -> int: ...


class InMemoryFacilityRepository:
    """Dict-backed FacilityRepository for tests and local development."""

    def __init__(self) -> None:
        self._rows: dict[UUID, Facility] = {}

    def upsert(self, facility: Facility) -> UUID:
        self._rows[facility.id] = facility
        return facility.id

    def upsert_many(self, facilities: list[Facility]) -> int:
        for f in facilities:
            self.upsert(f)
        return len(facilities)

    def load_geojson(self, path: Path = DEFAULT_GEOJSON_PATH, *, schema_path: Path | None = DEFAULT_SCHEMA_PATH) -> int:
        facilities = parse_geojson(path, schema_path=schema_path)
        n = self.upsert_many(facilities)
        log.info("facility-repo: loaded %d features from %s", n, path)
        return n

    def get(self, facility_id: UUID) -> Facility:
        try:
            return self._rows[facility_id]
        except KeyError:
            raise KeyError(f"Facility not found: id={facility_id}") from None

    def iter_by_country(self, country_iso3: str) -> Iterator[Facility]:
        for row in self._rows.values():
            if row.country == country_iso3:
                yield row

    def __len__(self) -> int:
        return len(self._rows)


def _row_to_facility(row: Any) -> Facility:
    """Convert a SQLAlchemy Row to a Facility Pydantic model."""
    return Facility(
        id=row.id,
        name=row.name,
        facility_type=FacilityType(row.facility_type),
        geometry_wkt=row.geometry_wkt,
        country=row.country,
        capacity_barrels=row.capacity_barrels,
        capacity_uncertainty_pct=row.capacity_uncertainty_pct,
        operator=row.operator,
        source_url=row.source_url,
        added_at=row.added_at,
        notes=row.notes,
    )


class PostgisFacilityRepository:
    """PostgreSQL + PostGIS backed FacilityRepository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, facility: Facility) -> UUID:
        """Insert or update a facility, returning its id."""
        stmt = pg_insert(models.facilities).values(
            id=facility.id,
            name=facility.name,
            facility_type=facility.facility_type.value,
            geometry=func.ST_GeomFromText(facility.geometry_wkt, 4326),
            country=facility.country,
            capacity_barrels=facility.capacity_barrels,
            capacity_uncertainty_pct=facility.capacity_uncertainty_pct,
            operator=facility.operator,
            source_url=facility.source_url,
            added_at=facility.added_at,
            notes=facility.notes,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={
                "name": facility.name,
                "facility_type": facility.facility_type.value,
                "geometry": func.ST_GeomFromText(facility.geometry_wkt, 4326),
                "country": facility.country,
                "capacity_barrels": facility.capacity_barrels,
                "capacity_uncertainty_pct": facility.capacity_uncertainty_pct,
                "operator": facility.operator,
                "source_url": facility.source_url,
                "added_at": facility.added_at,
                "notes": facility.notes,
            },
        )
        self._session.execute(stmt)
        self._session.flush()
        return facility.id

    def load_geojson(
        self,
        path: Path = DEFAULT_GEOJSON_PATH,
        *,
        schema_path: Path | None = DEFAULT_SCHEMA_PATH,
    ) -> int:
        """Parse, validate, and batch-upsert GeoJSON facilities."""
        facilities = parse_geojson(path, schema_path=schema_path)
        for f in facilities:
            self.upsert(f)
        log.info("facility-repo: loaded %d features from %s", len(facilities), path)
        return len(facilities)

    def get(self, facility_id: UUID) -> Facility:
        """Return one Facility by id; raises KeyError if absent."""
        t = models.facilities
        row = self._session.execute(
            select(
                t.c.id, t.c.name, t.c.facility_type,
                func.ST_AsText(t.c.geometry).label("geometry_wkt"),
                t.c.country, t.c.capacity_barrels, t.c.capacity_uncertainty_pct,
                t.c.operator, t.c.source_url, t.c.added_at, t.c.notes,
            ).where(t.c.id == facility_id)
        ).first()
        if row is None:
            raise KeyError(f"Facility not found: id={facility_id}")
        return _row_to_facility(row)

    def iter_by_country(self, country_iso3: str) -> Iterator[Facility]:
        """Yield every Facility whose country matches ``country_iso3``."""
        t = models.facilities
        result = self._session.execute(
            select(
                t.c.id, t.c.name, t.c.facility_type,
                func.ST_AsText(t.c.geometry).label("geometry_wkt"),
                t.c.country, t.c.capacity_barrels, t.c.capacity_uncertainty_pct,
                t.c.operator, t.c.source_url, t.c.added_at, t.c.notes,
            ).where(t.c.country == country_iso3)
        )
        for row in result:
            yield _row_to_facility(row)

    def __len__(self) -> int:
        result = self._session.execute(
            select(func.count()).select_from(models.facilities)
        )
        return result.scalar_one()
