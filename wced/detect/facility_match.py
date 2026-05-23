"""Attribute a CandidateFireEvent to the nearest registered Facility.

Spatial lookup uses a Shapely STRtree (R*-tree) built over facility geometries
for O(log n) candidate retrieval, followed by precise haversine-based distance
computation for the match threshold check.

Distance semantics:
  - Facility is a Point: haversine distance from candidate centroid to the
    facility point.
  - Facility is a Polygon: haversine distance from candidate centroid to the
    nearest point on the polygon boundary. If the centroid lies inside the
    polygon the distance is 0 m.

Methodology reference: methodology/v1.0.pdf §3.2 — "Facility Attribution".
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from shapely import STRtree
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points

from wced.detect.hotspot import CandidateFireEvent
from wced.models.facility import Facility
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord
from wced.provenance.store import ProvenanceStore

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD_M: Final[float] = 500.0
_EARTH_RADIUS_M: Final[float] = 6_371_000.0


# ---------------------------------------------------------------------------
# Distance helpers
# ---------------------------------------------------------------------------


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in metres between two WGS84 points.

    Parameters
    ----------
    lat1, lon1 : float
        First point in decimal degrees.
    lat2, lon2 : float
        Second point in decimal degrees.

    Returns
    -------
    float
        Great-circle distance in metres.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2.0 * _EARTH_RADIUS_M * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _nearest_point_on_geom(geom: BaseGeometry, lat: float, lon: float) -> tuple[float, float]:
    """Return the (lat, lon) of the nearest point on *geom* to the query location.

    Parameters
    ----------
    geom : BaseGeometry
        Facility geometry (Point or Polygon) in WGS84.
    lat, lon : float
        Query location in decimal degrees.

    Returns
    -------
    tuple[float, float]
        (latitude, longitude) of the nearest point on *geom*, in degrees.
        For a Point facility this is the facility point itself.
        For a Polygon this is the nearest boundary point, or the query
        point itself if it lies inside the polygon (distance = 0).
    """
    query_pt = Point(lon, lat)  # Shapely uses (x=lon, y=lat)
    if geom.geom_type == "Point":
        return geom.y, geom.x
    # nearest_points(geom, query_pt) returns (point_on_geom, query_pt).
    # Shapely treats filled polygons, so the nearest point on a polygon for
    # an interior query is the query point itself → haversine distance = 0.
    near, _ = nearest_points(geom, query_pt)
    return near.y, near.x


def distance_to_facility_m(
    candidate_lat: float,
    candidate_lon: float,
    facility: Facility,
) -> float:
    """Haversine distance in metres from a point to the nearest point on a Facility.

    Parameters
    ----------
    candidate_lat, candidate_lon : float
        Candidate centroid in WGS84 decimal degrees.
    facility : Facility
        Registered facility whose geometry is interrogated.

    Returns
    -------
    float
        Distance in metres (0 if the candidate is inside a polygon facility).
    """
    geom = facility.geometry()
    near_lat, near_lon = _nearest_point_on_geom(geom, candidate_lat, candidate_lon)
    return _haversine_m(candidate_lat, candidate_lon, near_lat, near_lon)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_to_facility(
    candidate: CandidateFireEvent,
    facilities: list[Facility],
    *,
    threshold_m: float = DEFAULT_THRESHOLD_M,
    store: ProvenanceStore,
    produced_at: datetime | None = None,
) -> tuple[Facility | None, float]:
    """Attribute *candidate* to the nearest Facility within *threshold_m* metres.

    Uses an STRtree for fast nearest-geometry lookup (coordinate-space), then
    computes the precise haversine distance to confirm the match. If the nearest
    facility is beyond *threshold_m* the candidate is unmatched.

    A ProvenanceRecord is always emitted to *store*, regardless of whether a
    match is found.

    Parameters
    ----------
    candidate : CandidateFireEvent
        The clustered fire candidate to attribute.
    facilities : list[Facility]
        All registered facilities to search. May be empty.
    threshold_m : float
        Maximum candidate-to-facility distance for a positive match, in metres.
        Default 500 m.
    store : ProvenanceStore
        Receives one ProvenanceRecord recording the attribution decision.
    produced_at : datetime or None
        Wall-clock time for the ProvenanceRecord. Defaults to UTC now.

    Returns
    -------
    tuple[Facility | None, float]
        (matched_facility, match_distance_m).
        If no facility is within *threshold_m*, matched_facility is None and
        match_distance_m is the distance to the nearest facility (or inf if
        *facilities* is empty).
    """
    ts = produced_at or datetime.now(tz=UTC)

    if not facilities:
        _emit_no_match_provenance(candidate, None, float("inf"), threshold_m, store, ts)
        return None, float("inf")

    geometries: list[BaseGeometry] = [f.geometry() for f in facilities]
    tree = STRtree(geometries)

    # STRtree.nearest returns the index of the geometrically nearest item in
    # the native coordinate space (degrees). Accurate enough as a prefilter for
    # the 500 m scale; the haversine check below is the authoritative distance.
    centroid_pt = Point(candidate.centroid_lon, candidate.centroid_lat)
    nearest_idx = int(tree.nearest(centroid_pt))

    nearest_facility = facilities[nearest_idx]
    dist_m = distance_to_facility_m(
        candidate.centroid_lat,
        candidate.centroid_lon,
        nearest_facility,
    )

    matched = nearest_facility if dist_m <= threshold_m else None

    rec = ProvenanceRecord(
        produced_by="wced.detect.facility_match",
        inputs=[candidate.provenance_id],
        method="strtree_nearest_haversine_v1.0",
        parameters={
            "threshold_m": threshold_m,
            "match_distance_m": round(dist_m, 2),
            "matched": matched is not None,
            "facility_id": str(nearest_facility.id) if matched else None,
        },
        produced_at=ts,
        confidence_label=ConfidenceLabel.SUSPECTED,
        notes=None,
    )
    store.record_provenance(rec)

    if matched:
        log.info(
            "facility_match: candidate %s → facility %s (%.0f m)",
            candidate.id,
            nearest_facility.id,
            dist_m,
        )
    else:
        log.info(
            "facility_match: candidate %s unmatched (nearest=%.0f m > %.0f m threshold)",
            candidate.id,
            dist_m,
            threshold_m,
        )

    return matched, dist_m


def _emit_no_match_provenance(
    candidate: CandidateFireEvent,
    facility: Facility | None,
    dist_m: float,
    threshold_m: float,
    store: ProvenanceStore,
    ts: datetime,
) -> None:
    """Emit a ProvenanceRecord for an unmatched or degenerate attribution."""
    rec = ProvenanceRecord(
        produced_by="wced.detect.facility_match",
        inputs=[candidate.provenance_id],
        method="strtree_nearest_haversine_v1.0",
        parameters={
            "threshold_m": threshold_m,
            "match_distance_m": dist_m,
            "matched": False,
            "facility_id": None,
            "reason": "no_facilities" if facility is None else "beyond_threshold",
        },
        produced_at=ts,
        confidence_label=ConfidenceLabel.SUSPECTED,
        notes="No registered facilities available for matching"
        if facility is None
        else None,
    )
    store.record_provenance(rec)


def build_facility_tree(facilities: list[Facility]) -> tuple[STRtree, list[Facility]]:
    """Build a reusable STRtree over a facility list.

    For pipelines that call ``match_to_facility`` in a hot loop, pre-building
    the tree once avoids O(n) tree construction per candidate. Pass the tree
    directly to ``match_to_facility_with_tree`` instead of the list.

    Parameters
    ----------
    facilities : list[Facility]
        Registered facilities to index.

    Returns
    -------
    tuple[STRtree, list[Facility]]
        (tree, facilities) where tree[i] corresponds to facilities[i].
    """
    geometries = [f.geometry() for f in facilities]
    return STRtree(geometries), facilities


def match_to_facility_with_tree(
    candidate: CandidateFireEvent,
    tree: STRtree,
    facilities: list[Facility],
    *,
    threshold_m: float = DEFAULT_THRESHOLD_M,
    store: ProvenanceStore,
    produced_at: datetime | None = None,
) -> tuple[Facility | None, float]:
    """Attribute *candidate* using a pre-built STRtree.

    Same semantics as ``match_to_facility`` but avoids rebuilding the tree
    for each candidate. Use ``build_facility_tree`` to construct the tree.

    Parameters
    ----------
    candidate : CandidateFireEvent
        The clustered fire candidate to attribute.
    tree : STRtree
        Pre-built spatial index over *facilities*.
    facilities : list[Facility]
        Must correspond 1-to-1 with the geometries indexed in *tree*.
    threshold_m : float
        Match distance threshold in metres.
    store : ProvenanceStore
        Receives one ProvenanceRecord per call.
    produced_at : datetime or None
        Defaults to UTC now.

    Returns
    -------
    tuple[Facility | None, float]
        (matched_facility, match_distance_m).
    """
    ts = produced_at or datetime.now(tz=UTC)

    if not facilities:
        _emit_no_match_provenance(candidate, None, float("inf"), threshold_m, store, ts)
        return None, float("inf")

    centroid_pt = Point(candidate.centroid_lon, candidate.centroid_lat)
    nearest_idx = int(tree.nearest(centroid_pt))
    nearest_facility = facilities[nearest_idx]
    dist_m = distance_to_facility_m(
        candidate.centroid_lat,
        candidate.centroid_lon,
        nearest_facility,
    )
    matched = nearest_facility if dist_m <= threshold_m else None

    rec = ProvenanceRecord(
        produced_by="wced.detect.facility_match",
        inputs=[candidate.provenance_id],
        method="strtree_nearest_haversine_v1.0",
        parameters={
            "threshold_m": threshold_m,
            "match_distance_m": round(dist_m, 2),
            "matched": matched is not None,
            "facility_id": str(nearest_facility.id) if matched else None,
        },
        produced_at=ts,
        confidence_label=ConfidenceLabel.SUSPECTED,
        notes=None,
    )
    store.record_provenance(rec)

    return matched, dist_m
