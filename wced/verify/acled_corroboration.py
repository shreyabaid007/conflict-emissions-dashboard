"""Spatial and temporal matching of ACLED events to fire candidates.

A candidate is *corroborated* by an ACLED event when the event falls within
a configurable time window and haversine distance of the candidate's centroid.
Matching uses the same haversine helper pattern as the rest of the detect stack
rather than introducing a PostGIS dependency here — sets are small enough that
an O(n) scan is fine.

ACLED only records event dates (midnight UTC), not sub-day times, so temporal
matching compares the candidate's ``first_detected_at`` against the span
[event_date - time_window_h, event_date + 24 h + time_window_h] rather than a
symmetric window around the event time.

Methodology reference: methodology/v1.0.pdf §4.3 — "Verification and
Confidence Labels", Table 4.

Attribution note: any downstream output that surfaces the returned ACLEDEvent
records MUST include the ACLED attribution string from
``wced.ingest.acled.ACLED_ATTRIBUTION``.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Final

from wced.detect.hotspot import CandidateFireEvent
from wced.ingest.acled import ACLEDEvent

log = logging.getLogger(__name__)

# Default search windows — methodology/v1.0.pdf §4.3, Table 4.
# Time: ACLED dates are day-resolution; ±24 h absorbs midnight boundary effects
# plus the configurable extension. Tune against Phase-5 ground truth.
DEFAULT_TIME_WINDOW_H: Final[float] = 24.0
DEFAULT_SPACE_WINDOW_M: Final[float] = 2_000.0

_EARTH_RADIUS_M: Final[float] = 6_371_000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in metres between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    )
    return 2.0 * _EARTH_RADIUS_M * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def find_acled_corroboration(
    candidate: CandidateFireEvent,
    acled_events: list[ACLEDEvent],
    *,
    time_window_h: float = DEFAULT_TIME_WINDOW_H,
    space_window_m: float = DEFAULT_SPACE_WINDOW_M,
) -> list[ACLEDEvent]:
    """Return ACLED events that spatially and temporally overlap a candidate.

    Matching criteria (both must hold):
    - **Temporal**: ``candidate.first_detected_at`` falls within the expanded
      window ``[event_date - time_window_h, event_date + 24h + time_window_h]``.
      The 24 h addend accounts for ACLED's day-only resolution — an event
      catalogued on event_date could have occurred at any point in that day.
    - **Spatial**: haversine distance between candidate centroid and ACLED
      event centroid ≤ ``space_window_m``.

    Note on false positives: a spatial/temporal match does NOT imply the ACLED
    event directly caused the fire. It is evidence that an armed event occurred
    nearby at roughly the right time. ``assign_confidence`` in
    ``wced.verify.confidence`` combines this match with satellite evidence
    before upgrading the confidence label.

    Parameters
    ----------
    candidate : CandidateFireEvent
        The clustered fire candidate to match against.
    acled_events : list[ACLEDEvent]
        Pre-fetched ACLED records to search. Callers are responsible for
        fetching a suitably wide window (country + date range) ahead of time;
        this function performs no I/O.
    time_window_h : float
        Hours to extend the search window beyond the ACLED day boundary on
        each side. Default 24 h (methodology §4.3, Table 4).
    space_window_m : float
        Maximum haversine distance (metres) for a spatial match.
        Default 2 000 m (methodology §4.3, Table 4).

    Returns
    -------
    list[ACLEDEvent]
        All events satisfying both criteria, sorted ascending by haversine
        distance from the candidate centroid so callers can prefer the
        closest match.
    """
    extension = timedelta(hours=time_window_h)
    candidate_t = candidate.first_detected_at
    matches: list[tuple[float, ACLEDEvent]] = []

    for event in acled_events:
        # Temporal check: expand the ACLED event_date by ±time_window_h plus
        # the full 24 h that the day boundary spans.
        event_start = event.detected_at - extension
        event_end = event.detected_at + timedelta(hours=24) + extension
        if not (event_start <= candidate_t <= event_end):
            continue

        dist_m = _haversine_m(
            candidate.centroid_lat,
            candidate.centroid_lon,
            event.latitude,
            event.longitude,
        )
        if dist_m <= space_window_m:
            matches.append((dist_m, event))

    matches.sort(key=lambda t: t[0])
    matched = [ev for _, ev in matches]
    log.debug(
        "find_acled_corroboration: candidate=%s matches=%d "
        "(time_window_h=%.0f space_window_m=%.0f)",
        candidate.id,
        len(matched),
        time_window_h,
        space_window_m,
    )
    return matched
