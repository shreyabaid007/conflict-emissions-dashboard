"""Fire hotspot clustering — converts raw FIRMS detections into CandidateFireEvents.

A CandidateFireEvent groups co-located hotspots from successive overpasses into
a single candidate event, ready for facility matching and persistence scoring.

Clustering strategy:
  1. Spatial DBSCAN with eps=500 m (haversine) and min_samples=1, so every pixel
     is a core point and there is no noise class.
  2. Temporal split: within each spatial cluster, a gap >24 h between consecutive
     acquisition times creates a new candidate (the fire may have extinguished
     and relit).

Methodology reference: methodology/v1.0.pdf §3.1 — "Hotspot Clustering and
Candidate Event Construction".
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from wced.models.event import DetectionSource
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord
from wced.provenance.store import ProvenanceStore

log = logging.getLogger(__name__)

_EARTH_RADIUS_M: Final[float] = 6_371_000.0

# DBSCAN parameters — methodology/v1.0.pdf §3.1
DEFAULT_EPS_M: Final[float] = 500.0
DEFAULT_TEMPORAL_GAP_H: Final[float] = 24.0
# min_samples=1: every pixel is a core point, so there is no noise label (-1).
_MIN_SAMPLES: Final[int] = 1


class FIRMSDetection(BaseModel):
    """A single thermal anomaly pixel from the NASA FIRMS area API.

    FIRMSDetection is the normalised form of one CSV row from FIRMSConnector,
    with sensor-specific brightness columns unified and FRP expressed in MW.

    Parameters
    ----------
    id : UUID
        Stable identifier. Pre-generate from the (source, acq_date, acq_time,
        lat, lon) tuple so re-ingesting the same file is idempotent.
    latitude, longitude : float
        Pixel centroid in WGS84 decimal degrees.
    frp_mw : float
        Fire Radiative Power in megawatts as reported by FIRMS.
    detected_at : AwareDatetime
        Acquisition timestamp (UTC).
    detection_source : DetectionSource
        Sensor that produced this pixel.
    brightness_k : float
        Brightness temperature in Kelvin (MODIS channel 21/22; VIIRS Ti4).
    confidence : str
        Raw FIRMS confidence field: "l"/"n"/"h" for VIIRS or a numeric string
        for MODIS. Kept as-is to avoid losing the VIIRS categorical coding.
    source_id : UUID
        ID of the Source provenance record from which this detection was
        extracted.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    frp_mw: float = Field(ge=0.0)
    detected_at: AwareDatetime
    detection_source: DetectionSource
    brightness_k: float = Field(ge=0.0)
    confidence: str
    source_id: UUID


class CandidateFireEvent(BaseModel):
    """A clustered group of co-located, temporally-continuous hotspot detections.

    Produced by ``hotspots_to_candidates``. Not yet attributed to a Facility;
    ``wced.detect.facility_match`` performs that step.

    Parameters
    ----------
    id : UUID
        Stable identifier for this candidate.
    hotspots : tuple[FIRMSDetection, ...]
        All constituent FIRMS pixels, sorted by detected_at. Non-empty.
    centroid_lat, centroid_lon : float
        Unweighted spatial centroid of the pixel coordinates (WGS84 degrees).
    first_detected_at : AwareDatetime
        Timestamp of the earliest constituent hotspot.
    last_detected_at : AwareDatetime
        Timestamp of the most recent constituent hotspot.
    peak_frp_mw : float
        Maximum FRP across all constituent hotspots, in MW.
    mean_frp_mw : float
        Arithmetic mean FRP across all constituent hotspots, in MW.
    n_overpasses : int
        Count of distinct acquisition timestamps. A candidate with
        n_overpasses >= 2 satisfies the basic persistence criterion.
    provenance_id : UUID
        ID of the ProvenanceRecord that produced this candidate.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    hotspots: tuple[FIRMSDetection, ...]
    centroid_lat: float
    centroid_lon: float
    first_detected_at: AwareDatetime
    last_detected_at: AwareDatetime
    peak_frp_mw: float = Field(ge=0.0)
    mean_frp_mw: float = Field(ge=0.0)
    n_overpasses: int = Field(ge=1)
    provenance_id: UUID


# ---------------------------------------------------------------------------
# Internal geometry helpers
# ---------------------------------------------------------------------------


def _haversine_distances_m(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Return the (n, n) pairwise haversine distance matrix in metres.

    Parameters
    ----------
    lats, lons : np.ndarray
        1-D arrays of WGS84 latitude and longitude in decimal degrees,
        both length n.

    Returns
    -------
    np.ndarray
        Shape (n, n). Entry [i, j] is the haversine distance in metres
        between point i and point j.
    """
    lat_r = np.deg2rad(lats)[:, None]
    lon_r = np.deg2rad(lons)[:, None]
    lat_r2 = np.deg2rad(lats)[None, :]
    lon_r2 = np.deg2rad(lons)[None, :]
    dlat = lat_r2 - lat_r
    dlon = lon_r2 - lon_r
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(lat_r) * np.cos(lat_r2) * np.sin(dlon / 2) ** 2
    )
    return 2.0 * _EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _dbscan_labels(dist_m: np.ndarray, eps_m: float) -> list[int]:
    """Run DBSCAN on a precomputed distance matrix and return cluster labels.

    With min_samples=1 every point is its own core point, so the algorithm
    degenerates to finding connected components in the eps-neighbourhood graph.
    There are no noise points (-1 labels) in the output.

    Parameters
    ----------
    dist_m : np.ndarray
        Square (n, n) pairwise distance matrix in metres.
    eps_m : float
        Neighbourhood radius in metres.

    Returns
    -------
    list[int]
        Cluster label for each of the n points. Labels are contiguous
        integers starting at 0.
    """
    n = dist_m.shape[0]
    labels = [-1] * n
    cluster_id = 0

    for i in range(n):
        if labels[i] != -1:
            continue
        labels[i] = cluster_id
        queue: list[int] = [i]
        head = 0
        while head < len(queue):
            p = queue[head]
            head += 1
            for j in range(n):
                if labels[j] == -1 and dist_m[p, j] <= eps_m:
                    labels[j] = cluster_id
                    queue.append(j)
        cluster_id += 1

    return labels


def _build_candidate(
    hotspots: list[FIRMSDetection],
    provenance_id: UUID,
) -> CandidateFireEvent:
    """Construct a CandidateFireEvent from an already-clustered list of hotspots.

    Parameters
    ----------
    hotspots : list[FIRMSDetection]
        Non-empty list of hotspot pixels belonging to the same cluster.
    provenance_id : UUID
        ID of the ProvenanceRecord that produced this grouping.

    Returns
    -------
    CandidateFireEvent
        With computed centroid, FRP statistics, and overpass count.
    """
    if not hotspots:
        raise ValueError("_build_candidate requires at least one hotspot")

    ordered = sorted(hotspots, key=lambda h: h.detected_at)
    lats = [h.latitude for h in ordered]
    lons = [h.longitude for h in ordered]
    frps = [h.frp_mw for h in ordered]
    n_overpasses = len({h.detected_at for h in ordered})

    return CandidateFireEvent(
        hotspots=tuple(ordered),
        centroid_lat=float(np.mean(lats)),
        centroid_lon=float(np.mean(lons)),
        first_detected_at=ordered[0].detected_at,
        last_detected_at=ordered[-1].detected_at,
        peak_frp_mw=float(max(frps)),
        mean_frp_mw=float(np.mean(frps)),
        n_overpasses=n_overpasses,
        provenance_id=provenance_id,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def hotspots_to_candidates(
    hotspots: list[FIRMSDetection],
    *,
    eps_m: float = DEFAULT_EPS_M,
    temporal_gap_h: float = DEFAULT_TEMPORAL_GAP_H,
    store: ProvenanceStore,
    produced_at: datetime | None = None,
) -> list[CandidateFireEvent]:
    """Cluster FIRMS hotspots into CandidateFireEvents.

    Two-stage clustering:
      1. Spatial DBSCAN (haversine, eps_m, min_samples=1) groups pixels from
         the same fire regardless of which overpass they came from.
      2. Temporal split: within each spatial cluster, a gap > temporal_gap_h
         between consecutive acquisition times starts a new candidate.

    A ProvenanceRecord (confidence=SUSPECTED) is emitted to *store* for each
    candidate produced.

    Parameters
    ----------
    hotspots : list[FIRMSDetection]
        Raw detections from the FIRMS ingest layer.
    eps_m : float
        DBSCAN spatial epsilon in metres. Default 500 m per methodology §3.1.
    temporal_gap_h : float
        Maximum allowed gap in hours between successive overpasses before a
        spatial cluster is split. Default 24 h.
    store : ProvenanceStore
        Receives one ProvenanceRecord per candidate group.
    produced_at : datetime or None
        Wall-clock time for ProvenanceRecords. Defaults to UTC now.

    Returns
    -------
    list[CandidateFireEvent]
        One entry per spatially and temporally distinct fire candidate.
        Empty if *hotspots* is empty.
    """
    if not hotspots:
        return []

    ts = produced_at or datetime.now(tz=UTC)
    lats = np.array([h.latitude for h in hotspots])
    lons = np.array([h.longitude for h in hotspots])
    # Precompute pairwise haversine distances in metres before calling DBSCAN.
    # _dbscan_labels receives a distance matrix, never raw degree coordinates.
    # Euclidean distance on degrees at Iran's latitude (~32–36 °N) would shrink
    # the effective E–W eps by cos(35°) ≈ 18 %, producing asymmetric clustering
    # that understates E–W overlap and misses edge cases near the threshold.
    dist_m = _haversine_distances_m(lats, lons)
    spatial_labels = _dbscan_labels(dist_m, eps_m)

    n_spatial = max(spatial_labels) + 1
    log.debug(
        "hotspots_to_candidates: %d hotspots → %d spatial clusters (eps_m=%.0f)",
        len(hotspots),
        n_spatial,
        eps_m,
    )

    candidates: list[CandidateFireEvent] = []
    gap = timedelta(hours=temporal_gap_h)
    groups_by_label: dict[int, list[FIRMSDetection]] = defaultdict(list)
    for hs, lbl in zip(hotspots, spatial_labels):
        groups_by_label[lbl].append(hs)

    for cluster_id in range(n_spatial):
        cluster_pts = sorted(groups_by_label[cluster_id], key=lambda h: h.detected_at)

        # Split on temporal gaps > temporal_gap_h.
        temporal_groups: list[list[FIRMSDetection]] = [[cluster_pts[0]]]
        for pt in cluster_pts[1:]:
            if pt.detected_at - temporal_groups[-1][-1].detected_at > gap:
                temporal_groups.append([])
            temporal_groups[-1].append(pt)

        for group in temporal_groups:
            rec = ProvenanceRecord(
                produced_by="wced.detect.hotspot",
                inputs=list({h.source_id for h in group}),
                method="dbscan_temporal_cluster_v1.0",
                parameters={
                    "eps_m": eps_m,
                    "temporal_gap_h": temporal_gap_h,
                    "n_hotspots": len(group),
                },
                produced_at=ts,
                confidence_label=ConfidenceLabel.SUSPECTED,
                notes=None,
            )
            store.record_provenance(rec)
            candidates.append(_build_candidate(group, rec.id))

    log.info(
        "hotspots_to_candidates: %d hotspots → %d candidates",
        len(hotspots),
        len(candidates),
    )
    return candidates
