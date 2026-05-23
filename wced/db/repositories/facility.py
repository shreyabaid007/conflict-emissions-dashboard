"""Facility repository — load the bootstrap GeoJSON into a registry store.

Two implementations share one Protocol:

- ``InMemoryFacilityRepository`` — dict-backed; used by tests and by any
  pipeline component that needs read access without booting Postgres.
- ``PostgisFacilityRepository`` — stubbed; mirrors the pattern established by
  ``wced.provenance.store.PostgresProvenanceStore``. The SQL that the
  full implementation will issue is documented in the method body so the
  schema migration prompt can fill it in without rediscovering intent.

Both implementations expose ``load_geojson(path)`` so callers can bring the
bootstrap file (``data/facilities/iran_oil_gas.geojson``) online at startup
without writing GeoJSON-parsing logic at the call site. The loader is
schema-validated — any drift between the file and ``facilities.schema.json``
raises before a single row reaches the store.

Production facility additions never flow through this loader. They enter via
the editorial workflow (PR + Scientific Steering Committee review) so that
every registry change is reviewable in git history; the bootstrap is a
one-time operation.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from jsonschema import Draft202012Validator
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from wced.models.facility import Facility, FacilityType

log = logging.getLogger(__name__)

# Default schema path, kept here (rather than imported from scripts/) so that
# the repository has no dependency on the bootstrap tooling.
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "data" / "facilities" / "facilities.schema.json"
DEFAULT_GEOJSON_PATH = Path(__file__).resolve().parents[3] / "data" / "facilities" / "iran_oil_gas.geojson"


# ---------------------------------------------------------------------------
# GeoJSON → Facility conversion
# ---------------------------------------------------------------------------


def _feature_to_facility(feature: dict[str, Any]) -> Facility:
    """Map one validated GeoJSON Feature dict onto a ``Facility``.

    The feature's top-level ``id`` becomes the Facility UUID so re-imports
    produce stable primary keys downstream (the bootstrap script generates
    these via uuid5 — see ``scripts.bootstrap_facilities.facility_uuid``).

    Raises
    ------
    ValueError
        If the feature lacks an ``id`` (the schema currently makes ``id``
        optional, but the loader requires it — every persisted Facility must
        have a stable UUID).
    """
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
    """Read a facility GeoJSON, schema-validate it, and return Facility rows.

    Validation is mandatory for the bootstrap file: a malformed registry must
    fail loudly at startup rather than silently producing partial detections
    later. Pass ``schema_path=None`` only in unit tests that exercise the
    parsing path with synthetic data.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if schema_path is not None:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(payload)
    return [_feature_to_facility(f) for f in payload["features"]]


# ---------------------------------------------------------------------------
# Repository protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class FacilityRepository(Protocol):
    """Interface that every facility-registry backend must satisfy.

    The repository is read-mostly: the bootstrap path writes once at startup
    via ``load_geojson``, after which queries dominate. Mutation outside of
    bootstrap goes through the editorial workflow, not through this Protocol.
    """

    def upsert(self, facility: Facility) -> UUID:
        """Insert or replace a Facility row; return its id."""
        ...

    def load_geojson(
        self,
        path: Path = DEFAULT_GEOJSON_PATH,
        *,
        schema_path: Path | None = DEFAULT_SCHEMA_PATH,
    ) -> int:
        """Bootstrap-load every feature from a validated GeoJSON file.

        Returns
        -------
        int
            Number of features inserted/replaced.
        """
        ...

    def get(self, facility_id: UUID) -> Facility:
        """Return one Facility by id; raises KeyError if absent."""
        ...

    def iter_by_country(self, country_iso3: str) -> Iterator[Facility]:
        """Yield every Facility whose country matches ``country_iso3``."""
        ...

    def __len__(self) -> int:
        """Number of facilities currently in the store."""
        ...


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class InMemoryFacilityRepository:
    """Dict-backed FacilityRepository for tests and local development.

    Not thread-safe. ``upsert`` is idempotent on ``Facility.id`` — re-inserting
    the same UUID replaces the previous row, matching the semantics the
    PostGIS backend will provide via ``ON CONFLICT (id) DO UPDATE``.
    """

    def __init__(self) -> None:
        self._rows: dict[UUID, Facility] = {}

    def upsert(self, facility: Facility) -> UUID:
        self._rows[facility.id] = facility
        return facility.id

    def upsert_many(self, facilities: Iterable[Facility]) -> int:
        """Batched upsert — convenience used by ``load_geojson``."""
        count = 0
        for f in facilities:
            self.upsert(f)
            count += 1
        return count

    def load_geojson(
        self,
        path: Path = DEFAULT_GEOJSON_PATH,
        *,
        schema_path: Path | None = DEFAULT_SCHEMA_PATH,
    ) -> int:
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

    def __repr__(self) -> str:
        return f"InMemoryFacilityRepository(rows={len(self._rows)})"


# ---------------------------------------------------------------------------
# PostGIS backend (stub)
# ---------------------------------------------------------------------------


# DDL applied by the schema migration that paves the way for this repository.
# Kept here as a string so that the migration prompt has an authoritative
# reference; the migration itself owns the actual Alembic up/down logic.
POSTGIS_DDL: str = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS facility (
    id                       UUID PRIMARY KEY,
    name                     TEXT NOT NULL,
    facility_type            TEXT NOT NULL,
    country                  CHAR(3) NOT NULL,
    geom                     GEOMETRY(Geometry, 4326) NOT NULL,
    capacity_barrels         DOUBLE PRECISION,
    capacity_uncertainty_pct DOUBLE PRECISION NOT NULL DEFAULT 30.0,
    operator                 TEXT,
    source_url               TEXT NOT NULL,
    added_at                 TIMESTAMPTZ NOT NULL,
    notes                    TEXT
);

CREATE INDEX IF NOT EXISTS facility_geom_gix    ON facility USING GIST (geom);
CREATE INDEX IF NOT EXISTS facility_country_idx ON facility (country);
"""


class PostgisFacilityRepository:
    """PostgreSQL + PostGIS backed FacilityRepository.

    Stubbed pending the database prompt: the methods document the SQL they
    will issue so the implementing prompt can fill them in without
    rediscovering the schema. The interface is identical to
    ``InMemoryFacilityRepository`` so call sites can swap backends.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def upsert(self, facility: Facility) -> UUID:
        """
        INSERT INTO facility (id, name, facility_type, country, geom, ...)
        VALUES (%s, %s, %s, %s, ST_GeomFromText(%s, 4326), ...)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            facility_type = EXCLUDED.facility_type,
            ...
        RETURNING id;
        """
        raise NotImplementedError("PostgisFacilityRepository not yet implemented")

    def load_geojson(
        self,
        path: Path = DEFAULT_GEOJSON_PATH,
        *,
        schema_path: Path | None = DEFAULT_SCHEMA_PATH,
    ) -> int:
        """Parse + validate + batch-COPY the GeoJSON into the ``facility`` table.

        Production implementation should wrap the COPY in a single transaction
        so a malformed feature near the end of the file does not leave the
        registry in a half-loaded state.
        """
        raise NotImplementedError("PostgisFacilityRepository not yet implemented")

    def get(self, facility_id: UUID) -> Facility:
        """``SELECT … FROM facility WHERE id = %s`` with ST_AsText(geom)."""
        raise NotImplementedError("PostgisFacilityRepository not yet implemented")

    def iter_by_country(self, country_iso3: str) -> Iterator[Facility]:
        """``SELECT … FROM facility WHERE country = %s`` — server-side cursor."""
        raise NotImplementedError("PostgisFacilityRepository not yet implemented")

    def __len__(self) -> int:
        """``SELECT count(*) FROM facility``."""
        raise NotImplementedError("PostgisFacilityRepository not yet implemented")
