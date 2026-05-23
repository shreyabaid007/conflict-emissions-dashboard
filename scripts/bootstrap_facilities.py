"""One-time bootstrap of the WCED Iran/Gulf facility registry.

Sources merged by this script
-----------------------------
1. **Curated seed** (embedded in this file): a hand-vetted list of major
   oil/fuel/gas infrastructure covering the V1 priority targets:
   Tehran Refinery + Shahr-e Rey, Shahran depot, Aghdasieh depot, Fardis
   (Karaj), Bandar Abbas refinery + naval base, Lavan Island, Kharg Island,
   Haifa and Ashdod refineries (Israel), BAPCO Sitra (Bahrain), Ras Laffan
   (Qatar).
2. **Global Energy Monitor Oil & Gas Plant Tracker** CSV export, when a path
   is supplied via ``--gem-csv``. Rows are joined onto the seed by GEM ID;
   unmatched rows in the seed's country set are appended as new features.
3. **OpenStreetMap via Overpass API**, when ``--overpass`` is passed. Used to
   enrich Point-only seed entries with a polygon footprint derived from the
   nearest ``industrial=oil_refinery`` / ``man_made=storage_tank`` element.

Every feature is validated against ``data/facilities/facilities.schema.json``
before being written. Production updates do **not** go through this script —
they enter via the editorial workflow (PR + Scientific Steering Committee).

Usage
-----
::

    python -m scripts.bootstrap_facilities \\
        --output data/facilities/iran_oil_gas.geojson \\
        [--gem-csv path/to/gem.csv] [--overpass]

Re-running with the same inputs produces a deterministic output: feature IDs
are uuid5(DNS, "wced.org/facility/<slug>") so import-then-re-import yields
the same primary keys downstream.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
import typer
from jsonschema import Draft202012Validator

log = logging.getLogger(__name__)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT: Final[Path] = REPO_ROOT / "data" / "facilities" / "iran_oil_gas.geojson"
SCHEMA_PATH: Final[Path] = REPO_ROOT / "data" / "facilities" / "facilities.schema.json"

# Stable namespace for uuid5 facility IDs. DNS-namespaced under wced.org so
# the same slug always resolves to the same UUID across reruns and machines.
_FACILITY_NAMESPACE: Final[uuid.UUID] = uuid.NAMESPACE_DNS

# Generator identifier persisted into the GeoJSON metadata and each feature's
# ``added_by`` field. Kept verbose so audit trails are unambiguous.
GENERATOR: Final[str] = "scripts/bootstrap_facilities.py"

OVERPASS_URL: Final[str] = "https://overpass-api.de/api/interpreter"

# Iran + Gulf bounding box; used to filter GEM rows and Overpass queries.
GULF_BBOX: Final[tuple[float, float, float, float]] = (25.0, 33.0, 44.0, 63.5)
# (south, west, north, east) — note Overpass uses S,W,N,E order while GeoJSON
# uses W,S,E,N. We store the Overpass order here because that is the only
# consumer of this constant in this module.

app = typer.Typer(help=__doc__.splitlines()[0], no_args_is_help=False, add_completion=False)


# ---------------------------------------------------------------------------
# Curated seed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedFacility:
    """One row in the embedded seed catalogue.

    Geometry is stored as a GeoJSON-shaped dict so it round-trips into the
    output file without intermediate conversion. ``slug`` participates in the
    uuid5 derivation and must remain stable across runs.
    """

    slug: str
    name: str
    facility_type: str
    country: str
    geometry: dict[str, Any]
    capacity_barrels: float | None
    source_url: str
    operator: str | None = None
    capacity_uncertainty_pct: float | None = None
    notes: str | None = None
    gem_id: str | None = None
    osm_id: str | None = None


def _rect(west: float, south: float, east: float, north: float) -> dict[str, Any]:
    """GeoJSON Polygon for an axis-aligned bounding rectangle (CCW exterior ring)."""
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }


def _point(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


# Curated seed. Coordinates are rounded to 3 decimal degrees (~110 m at these
# latitudes) deliberately — sub-100m precision triggers the dual-use review
# checklist in CLAUDE.md §Sensitive Areas. Polygons are coarse bounding
# rectangles; editorial review should replace them with surveyed footprints.
SEED: Final[tuple[SeedFacility, ...]] = (
    SeedFacility(
        slug="tehran-refinery-shahr-rey",
        name="Tehran Refinery (Shahr-e Rey)",
        facility_type="REFINERY",
        country="IRN",
        geometry=_rect(51.429, 35.584, 51.439, 35.594),
        capacity_barrels=250_000,
        capacity_uncertainty_pct=15.0,
        operator="NIORDC — Tehran Oil Refining Company",
        source_url="https://en.wikipedia.org/wiki/Tehran_Oil_Refining_Company",
        notes="Capacity is throughput in bpd. Polygon is an approximate bounding "
              "rectangle around the publicly-visible main process area; storage "
              "tanks to the north are captured by the separate Shahr-e Rey "
              "storage entry.",
    ),
    SeedFacility(
        slug="shahr-e-rey-storage",
        name="Shahr-e Rey storage depot",
        facility_type="STORAGE_TANK_FARM",
        country="IRN",
        geometry=_rect(51.439, 35.596, 51.449, 35.606),
        capacity_barrels=None,
        operator="NIOPDC",
        source_url="https://globalenergymonitor.org/projects/global-oil-gas-plant-tracker/",
        notes="Product storage tank farm adjacent to the Tehran Refinery. "
              "Tank-by-tank inventory is not publicly catalogued; capacity "
              "left null pending editorial review.",
    ),
    SeedFacility(
        slug="shahran-depot",
        name="Shahran fuel depot",
        facility_type="OIL_DEPOT",
        country="IRN",
        geometry=_point(51.310, 35.781),
        capacity_barrels=None,
        operator="NIOPDC",
        source_url="https://en.wikipedia.org/wiki/Shahran",
        notes="Major fuel distribution depot in northwest Tehran. Footprint "
              "not captured — stored as centroid pending OSM enrichment.",
    ),
    SeedFacility(
        slug="aghdasieh-depot",
        name="Aghdasieh fuel depot",
        facility_type="OIL_DEPOT",
        country="IRN",
        geometry=_point(51.493, 35.785),
        capacity_barrels=None,
        operator="NIOPDC",
        source_url="https://en.wikipedia.org/wiki/Aghdasieh",
        notes="Fuel storage depot in northeast Tehran. Footprint not captured "
              "— stored as centroid pending OSM enrichment.",
    ),
    SeedFacility(
        slug="fardis-karaj-depot",
        name="Fardis (Karaj) fuel depot",
        facility_type="OIL_DEPOT",
        country="IRN",
        geometry=_point(50.992, 35.738),
        capacity_barrels=None,
        operator="NIOPDC",
        source_url="https://en.wikipedia.org/wiki/Fardis",
        notes="Petroleum products depot serving the Karaj metro area west of "
              "Tehran. Centroid only.",
    ),
    SeedFacility(
        slug="bandar-abbas-refinery",
        name="Bandar Abbas Refinery",
        facility_type="REFINERY",
        country="IRN",
        geometry=_rect(56.177, 27.193, 56.190, 27.205),
        capacity_barrels=330_000,
        capacity_uncertainty_pct=15.0,
        operator="Persian Gulf Star Oil Company / NIORDC",
        source_url="https://en.wikipedia.org/wiki/Bandar_Abbas_Refinery",
        notes="Combined throughput of the main NIORDC refinery and the adjacent "
              "Persian Gulf Star condensate complex. Polygon is a coarse "
              "bounding rectangle.",
    ),
    SeedFacility(
        slug="bandar-abbas-naval-base",
        name="Bandar Abbas naval base",
        facility_type="STORAGE_TANK_FARM",
        country="IRN",
        geometry=_point(56.215, 27.130),
        capacity_barrels=None,
        operator="IRGN — Islamic Republic of Iran Navy",
        source_url="https://en.wikipedia.org/wiki/Bandar_Abbas",
        notes="Naval bunker fuel storage. Included because military fuel depots "
              "are within V1 scope when struck and observed burning; combat "
              "operations themselves are out of scope.",
    ),
    SeedFacility(
        slug="lavan-island-refinery",
        name="Lavan Island Refinery",
        facility_type="REFINERY",
        country="IRN",
        geometry=_rect(53.361, 26.810, 53.371, 26.818),
        capacity_barrels=55_000,
        capacity_uncertainty_pct=20.0,
        operator="Lavan Oil Refining Company / NIORDC",
        source_url="https://en.wikipedia.org/wiki/Lavan_Island",
        notes="Co-located with Lavan Island export terminal. Polygon covers "
              "refinery only; terminal infrastructure is on the south coast "
              "of the island and not separately catalogued.",
    ),
    SeedFacility(
        slug="kharg-island-terminal",
        name="Kharg Island crude export terminal",
        facility_type="TANKER_TERMINAL",
        country="IRN",
        geometry=_rect(50.310, 29.226, 50.328, 29.240),
        capacity_barrels=None,
        operator="NIOC — National Iranian Oil Company",
        source_url="https://en.wikipedia.org/wiki/Kharg_Island",
        notes="Iran's primary crude export terminal; handles the majority of "
              "seaborne exports. Polygon covers the main storage tank farm on "
              "the eastern side of the island.",
    ),
    SeedFacility(
        slug="haifa-bazan-refinery",
        name="Haifa Refineries (Bazan)",
        facility_type="REFINERY",
        country="ISR",
        geometry=_rect(35.034, 32.783, 35.047, 32.794),
        capacity_barrels=197_000,
        capacity_uncertainty_pct=10.0,
        operator="Bazan Group",
        source_url="https://en.wikipedia.org/wiki/Oil_Refineries_Ltd",
        notes="Largest refinery complex in Israel. Polygon covers the main "
              "refining area in Haifa Bay; the petrochemical complex to the "
              "south-east is treated separately when added.",
    ),
    SeedFacility(
        slug="ashdod-paz-refinery",
        name="Ashdod Refinery (Paz)",
        facility_type="REFINERY",
        country="ISR",
        geometry=_rect(34.640, 31.766, 34.652, 31.775),
        capacity_barrels=120_000,
        capacity_uncertainty_pct=10.0,
        operator="Paz Oil Company",
        source_url="https://en.wikipedia.org/wiki/Paz_Oil_Company",
        notes="Second of Israel's two crude refineries. Coarse bounding polygon.",
    ),
    SeedFacility(
        slug="bapco-sitra-refinery",
        name="BAPCO Sitra Refinery",
        facility_type="REFINERY",
        country="BHR",
        geometry=_rect(50.610, 26.152, 50.624, 26.164),
        capacity_barrels=267_000,
        capacity_uncertainty_pct=10.0,
        operator="Bahrain Petroleum Company (BAPCO)",
        source_url="https://en.wikipedia.org/wiki/Bahrain_Petroleum_Company",
        notes="Included per CEOBS WISEN public list of Gulf-state critical "
              "infrastructure. Bahrain's only oil refinery.",
    ),
    SeedFacility(
        slug="ras-laffan-industrial-city",
        name="Ras Laffan Industrial City",
        facility_type="GAS_PROCESSING",
        country="QAT",
        geometry=_rect(51.535, 25.900, 51.565, 25.925),
        capacity_barrels=None,
        operator="QatarEnergy",
        source_url="https://en.wikipedia.org/wiki/Ras_Laffan_Industrial_City",
        notes="Aggregated entry for the Ras Laffan LNG / GTL / gas processing "
              "complex. Capacity in barrels is not the natural unit for an LNG "
              "hub; left null pending the natural-gas emission factor "
              "(deferred per CLAUDE.md). Polygon is a coarse bounding rectangle.",
    ),
)


# ---------------------------------------------------------------------------
# Feature assembly
# ---------------------------------------------------------------------------


def facility_uuid(slug: str) -> uuid.UUID:
    """Deterministic facility UUID — uuid5(DNS, 'wced.org/facility/<slug>')."""
    return uuid.uuid5(_FACILITY_NAMESPACE, f"wced.org/facility/{slug}")


def _seed_to_feature(seed: SeedFacility, added_at: datetime) -> dict[str, Any]:
    """Convert a SeedFacility row into a GeoJSON Feature dict.

    The output mirrors the structure validated by ``facilities.schema.json``;
    optional fields are emitted as ``null`` (not omitted) so re-imports do not
    have to disambiguate "missing" from "explicitly unknown".
    """
    properties: dict[str, Any] = {
        "name": seed.name,
        "facility_type": seed.facility_type,
        "country": seed.country,
        "capacity_barrels": seed.capacity_barrels,
        "operator": seed.operator,
        "source_url": seed.source_url,
        "added_at": added_at.isoformat().replace("+00:00", "Z"),
        "added_by": GENERATOR,
        "gem_id": seed.gem_id,
        "osm_id": seed.osm_id,
        "notes": seed.notes,
    }
    if seed.capacity_uncertainty_pct is not None:
        properties["capacity_uncertainty_pct"] = seed.capacity_uncertainty_pct
    return {
        "type": "Feature",
        "id": str(facility_uuid(seed.slug)),
        "geometry": seed.geometry,
        "properties": properties,
    }


# ---------------------------------------------------------------------------
# Global Energy Monitor CSV merge
# ---------------------------------------------------------------------------


# GEM publishes their column names with spaces and mixed case. The mapping
# below targets the Oil & Gas Plant Tracker schema as of 2026-Q1. Unknown
# columns are tolerated — the script reads only the keys listed here.
_GEM_COLS: Final[dict[str, str]] = {
    "id": "GEM unit/phase ID",
    "name": "Unit name",
    "country": "Country/Area",
    "capacity": "Capacity (bbl/d)",
    "lat": "Latitude",
    "lon": "Longitude",
    "operator": "Operator",
    "facility_type": "Unit type",
}

# GEM 'Unit type' values that map onto our enum.
_GEM_TYPE_MAP: Final[dict[str, str]] = {
    "Crude oil refinery": "REFINERY",
    "Oil refinery": "REFINERY",
    "Gas processing plant": "GAS_PROCESSING",
    "LNG terminal": "GAS_PROCESSING",
    "Petrochemical": "PETROCHEMICAL",
    "Oil terminal": "TANKER_TERMINAL",
}

# ISO 3166-1 alpha-2 / common-name → alpha-3 for the small set of countries
# in scope. Keeps the dependency surface small (no pycountry).
_COUNTRY_ALPHA3: Final[dict[str, str]] = {
    "Iran": "IRN",
    "Israel": "ISR",
    "Bahrain": "BHR",
    "Qatar": "QAT",
    "United Arab Emirates": "ARE",
    "Saudi Arabia": "SAU",
    "Kuwait": "KWT",
    "Iraq": "IRQ",
    "Oman": "OMN",
}


def _country_to_iso3(name: str) -> str | None:
    """Map a GEM country-name string to ISO 3166-1 alpha-3 or None if unknown."""
    return _COUNTRY_ALPHA3.get(name.strip())


def load_gem_rows(csv_path: Path) -> list[SeedFacility]:
    """Parse a GEM Oil & Gas Plant Tracker CSV export.

    Only rows whose country is in our Gulf scope AND whose unit type maps to
    a WCED FacilityType are returned. Other rows are skipped with a debug
    log entry; this is intentional — V1 covers a narrow set of categories.

    Parameters
    ----------
    csv_path : Path
        Local CSV file downloaded from
        https://globalenergymonitor.org/projects/global-oil-gas-plant-tracker/.

    Returns
    -------
    list[SeedFacility]
        Each row is wrapped in SeedFacility for uniform handling with the
        embedded seed. Slugs are ``"gem-<id>"`` for deterministic UUIDs.
    """
    out: list[SeedFacility] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            country_iso3 = _country_to_iso3(row.get(_GEM_COLS["country"], ""))
            if country_iso3 is None:
                continue
            facility_type = _GEM_TYPE_MAP.get(row.get(_GEM_COLS["facility_type"], "").strip())
            if facility_type is None:
                continue
            try:
                lon = float(row[_GEM_COLS["lon"]])
                lat = float(row[_GEM_COLS["lat"]])
            except (KeyError, ValueError):
                log.warning("gem: row %s missing/non-numeric coords", row.get(_GEM_COLS["id"]))
                continue
            capacity_raw = row.get(_GEM_COLS["capacity"], "").strip()
            try:
                capacity = float(capacity_raw) if capacity_raw else None
            except ValueError:
                capacity = None
            gem_id = row[_GEM_COLS["id"]].strip()
            out.append(SeedFacility(
                slug=f"gem-{gem_id}",
                name=row[_GEM_COLS["name"]].strip(),
                facility_type=facility_type,
                country=country_iso3,
                geometry=_point(round(lon, 3), round(lat, 3)),
                capacity_barrels=capacity,
                operator=row.get(_GEM_COLS["operator"]) or None,
                source_url="https://globalenergymonitor.org/projects/global-oil-gas-plant-tracker/",
                notes=f"Imported from GEM Oil & Gas Plant Tracker row {gem_id}.",
                gem_id=gem_id,
            ))
    return out


def merge_gem_into_seed(
    seed: Sequence[SeedFacility],
    gem_rows: Sequence[SeedFacility],
) -> list[SeedFacility]:
    """Combine seed + GEM rows; deduplicate by gem_id.

    A GEM row whose ``gem_id`` matches an existing seed entry's ``gem_id`` is
    skipped — the seed takes precedence because it has been hand-curated.
    All other GEM rows are appended.
    """
    seed_gem_ids = {s.gem_id for s in seed if s.gem_id}
    merged = list(seed)
    for row in gem_rows:
        if row.gem_id and row.gem_id in seed_gem_ids:
            log.info("gem: skipping %s (already in seed)", row.gem_id)
            continue
        merged.append(row)
    return merged


# ---------------------------------------------------------------------------
# Overpass enrichment
# ---------------------------------------------------------------------------


@dataclass
class OverpassClient:
    """Thin httpx wrapper around the Overpass API for footprint lookups."""

    url: str = OVERPASS_URL
    timeout: float = 60.0
    user_agent: str = "wced-bootstrap/0.1 (https://wced.org)"

    def query(self, ql: str) -> dict[str, Any]:
        """POST a raw Overpass QL query and return the parsed JSON response."""
        with httpx.Client(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as c:
            r = c.post(self.url, data={"data": ql})
            r.raise_for_status()
            return r.json()


def enrich_with_overpass(
    seeds: Iterable[SeedFacility],
    client: OverpassClient | None = None,
) -> list[SeedFacility]:
    """Best-effort polygon enrichment for Point-only seeds via OSM Overpass.

    Only seeds whose current geometry is a Point are enriched. For each such
    seed, a 1-km radius Overpass query searches for the closest
    ``industrial=oil_refinery`` / ``man_made=storage_tank`` element; if found,
    the returned polygon replaces the Point. Failures are logged and the
    original seed is preserved.

    This is opt-in — the bootstrap default does not call Overpass so the
    canonical seed file is reproducible without network access.
    """
    client = client or OverpassClient()
    updated: list[SeedFacility] = []
    for seed in seeds:
        if seed.geometry.get("type") != "Point":
            updated.append(seed)
            continue
        lon, lat = seed.geometry["coordinates"]
        ql = (
            "[out:json][timeout:25];"
            f"(way[\"industrial\"=\"oil_refinery\"](around:1000,{lat},{lon});"
            f" way[\"man_made\"=\"storage_tank\"](around:1000,{lat},{lon}););"
            "out geom;"
        )
        try:
            response = client.query(ql)
        except httpx.HTTPError as exc:
            log.warning("overpass: %s failed (%s) — keeping point", seed.slug, exc)
            updated.append(seed)
            continue
        polygon = _overpass_first_polygon(response)
        if polygon is None:
            updated.append(seed)
            continue
        log.info("overpass: enriched %s with %d-vertex polygon",
                 seed.slug, len(polygon["coordinates"][0]))
        updated.append(_with_geometry(seed, polygon, osm_id=_overpass_first_osm_id(response)))
    return updated


def _overpass_first_polygon(response: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first closed way in an Overpass response as a GeoJSON Polygon."""
    for element in response.get("elements", []):
        if element.get("type") != "way":
            continue
        geom = element.get("geometry") or []
        if len(geom) < 4:
            continue
        ring = [[pt["lon"], pt["lat"]] for pt in geom]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        return {"type": "Polygon", "coordinates": [ring]}
    return None


def _overpass_first_osm_id(response: dict[str, Any]) -> str | None:
    for element in response.get("elements", []):
        if element.get("type") == "way" and "id" in element:
            return f"way/{element['id']}"
    return None


def _with_geometry(
    seed: SeedFacility,
    geometry: dict[str, Any],
    *,
    osm_id: str | None,
) -> SeedFacility:
    """Frozen-dataclass update helper — replace geometry and osm_id."""
    return SeedFacility(
        slug=seed.slug,
        name=seed.name,
        facility_type=seed.facility_type,
        country=seed.country,
        geometry=geometry,
        capacity_barrels=seed.capacity_barrels,
        source_url=seed.source_url,
        operator=seed.operator,
        capacity_uncertainty_pct=seed.capacity_uncertainty_pct,
        notes=seed.notes,
        gem_id=seed.gem_id,
        osm_id=osm_id or seed.osm_id,
    )


# ---------------------------------------------------------------------------
# Assemble + validate + write
# ---------------------------------------------------------------------------


def build_collection(
    seeds: Sequence[SeedFacility],
    *,
    added_at: datetime,
) -> dict[str, Any]:
    """Assemble a complete GeoJSON FeatureCollection from seed rows."""
    features = [_seed_to_feature(s, added_at) for s in seeds]
    return {
        "type": "FeatureCollection",
        "name": "iran_oil_gas",
        "metadata": {
            "schema": "facilities.schema.json",
            "generator": GENERATOR,
            "methodology_version": "v1.0",
            "generated_at": added_at.isoformat().replace("+00:00", "Z"),
            "coordinate_notes": (
                "Centroids rounded to ~3 decimal degrees (~110 m). Sub-100m "
                "precision triggers the dual-use review checklist "
                "(CLAUDE.md §Sensitive Areas)."
            ),
            "sources": [
                "https://globalenergymonitor.org/projects/global-oil-gas-plant-tracker/",
                "https://ceobs.org/the-wisen-tool/",
                "https://www.openstreetmap.org/",
            ],
        },
        "features": features,
    }


def validate(collection: dict[str, Any], schema_path: Path = SCHEMA_PATH) -> None:
    """Raise ``jsonschema.ValidationError`` on the first schema violation."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(collection)


@app.command()
def main(
    output: Path = typer.Option(DEFAULT_OUTPUT, "--output", "-o", help="GeoJSON output path."),
    gem_csv: Path | None = typer.Option(
        None,
        "--gem-csv",
        help="Optional GEM Oil & Gas Plant Tracker CSV export to merge.",
    ),
    overpass: bool = typer.Option(
        False,
        "--overpass/--no-overpass",
        help="If set, enrich Point-only seeds with polygon footprints via "
             "the OSM Overpass API (requires network).",
    ),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """Build, validate, and write the WCED facility registry GeoJSON."""
    logging.basicConfig(level=log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    seeds: list[SeedFacility] = list(SEED)
    if gem_csv is not None:
        log.info("gem: reading %s", gem_csv)
        seeds = merge_gem_into_seed(seeds, load_gem_rows(gem_csv))
    if overpass:
        log.info("overpass: enriching %d seeds", sum(1 for s in seeds if s.geometry["type"] == "Point"))
        seeds = enrich_with_overpass(seeds)

    collection = build_collection(seeds, added_at=datetime.now(tz=UTC))
    validate(collection)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(collection, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("wrote %d features → %s", len(collection["features"]), output)


if __name__ == "__main__":
    app()
