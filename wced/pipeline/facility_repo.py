"""In-memory facility repository loaded from a GeoJSON file.

A thin wrapper around ``list[Facility]`` that handles GeoJSON parsing,
geometry conversion (GeoJSON → WKT), and stable ID derivation.

PostGIS-backed ``PostgresFacilityRepository`` is deferred to a later prompt.
This implementation is the V1 primary repository used by ``daily_ingest``.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5

from shapely.geometry import shape

from wced.models.facility import Facility, FacilityType

_DEFAULT_GEOJSON: Path = (
    Path(__file__).parent.parent.parent / "data" / "facilities" / "iran_oil_gas.geojson"
)


class FacilityLoadError(RuntimeError):
    """Raised when the GeoJSON cannot be parsed into valid Facility objects."""


class InMemoryFacilityRepository:
    """Dict-backed repository of Facility objects loaded from a GeoJSON file.

    Parameters
    ----------
    facilities : list[Facility]
        Pre-constructed facilities. Use ``load_geojson`` to populate from disk.
    """

    def __init__(self, facilities: list[Facility]) -> None:
        self._by_id: dict[UUID, Facility] = {f.id: f for f in facilities}

    # ------------------------------------------------------------------ class methods

    @classmethod
    def load_geojson(
        cls,
        path: Path = _DEFAULT_GEOJSON,
    ) -> InMemoryFacilityRepository:
        """Parse a GeoJSON FeatureCollection into a populated repository.

        Each Feature's ``properties`` must include at minimum:
        ``name``, ``facility_type``, ``country``, ``source_url``.

        If ``properties.id`` is present it is used as the Facility UUID.
        Otherwise a deterministic UUID is derived from ``source_url:name``
        via ``uuid5(NAMESPACE_URL, ...)`` so the same GeoJSON always yields
        the same IDs across process restarts.

        Parameters
        ----------
        path : Path
            Path to a GeoJSON FeatureCollection on disk.

        Returns
        -------
        InMemoryFacilityRepository

        Raises
        ------
        FacilityLoadError
            If the file is missing, malformed, or any feature fails Pydantic
            validation. All per-feature errors are collected and reported
            together rather than stopping at the first failure.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FacilityLoadError(f"Facility GeoJSON not found: {path}") from exc
        except (json.JSONDecodeError, OSError) as exc:
            raise FacilityLoadError(f"Cannot read facility GeoJSON {path}: {exc}") from exc

        features = raw.get("features", [])
        facilities: list[Facility] = []
        errors: list[str] = []

        for i, feature in enumerate(features):
            try:
                props = feature.get("properties") or {}
                geom_wkt = shape(feature["geometry"]).wkt

                raw_id = props.get("id")
                facility_id: UUID = (
                    UUID(str(raw_id))
                    if raw_id
                    else uuid5(
                        NAMESPACE_URL,
                        f"{props['source_url']}:{props['name']}",
                    )
                )

                added_at_raw: str | None = props.get("added_at")
                if added_at_raw:
                    added_at = datetime.fromisoformat(added_at_raw)
                    if added_at.tzinfo is None:
                        added_at = added_at.replace(tzinfo=UTC)
                else:
                    added_at = datetime.now(tz=UTC)

                facility = Facility(
                    id=facility_id,
                    name=props["name"],
                    facility_type=FacilityType(props["facility_type"]),
                    geometry_wkt=geom_wkt,
                    country=props["country"],
                    capacity_barrels=props.get("capacity_barrels"),
                    capacity_uncertainty_pct=float(
                        props.get("capacity_uncertainty_pct", 30.0)
                    ),
                    operator=props.get("operator"),
                    source_url=props["source_url"],
                    added_at=added_at,
                    notes=props.get("notes"),
                )
                facilities.append(facility)

            except Exception as exc:
                errors.append(f"Feature {i}: {exc}")

        if errors:
            raise FacilityLoadError(
                f"{len(errors)} feature(s) failed validation in {path}:\n"
                + "\n".join(f"  {e}" for e in errors)
            )

        return cls(facilities)

    # ------------------------------------------------------------------ public

    def all(self) -> list[Facility]:
        """Return all registered facilities in insertion order."""
        return list(self._by_id.values())

    def get(self, facility_id: UUID) -> Facility | None:
        """Return the facility with *facility_id*, or ``None`` if absent."""
        return self._by_id.get(facility_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def __repr__(self) -> str:
        return f"InMemoryFacilityRepository({len(self)} facilities)"
