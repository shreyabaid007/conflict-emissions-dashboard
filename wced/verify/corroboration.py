"""Source-agnostic spatial and temporal corroboration of fire candidates.

Matches conflict events from any supported source (ACLED, GDELT) to fire
candidates using haversine distance and temporal window overlap. This module
supersedes the ACLED-only ``acled_corroboration.py`` to support multiple
conflict-event sources with different corroboration strengths.

A candidate is *corroborated* by a conflict event when the event falls within
a configurable time window and haversine distance of the candidate's centroid.

ACLED events use day-only resolution; GDELT events similarly resolve to the
day of the article. Temporal matching expands the window accordingly.

Corroboration strength:
  - ACLED (human-reviewed) → can push confidence to CONFIRMED
  - GDELT (machine-extracted) → caps at VERIFIED (one tier below CONFIRMED)

See ``wced.verify.confidence`` for the full decision table.

Methodology reference: methodology/v1.0.pdf §4.3 — "Verification and
Confidence Labels", Table 4.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Union

from wced.detect.hotspot import CandidateFireEvent
from wced.ingest.acled import ACLEDEvent
from wced.ingest.gdelt import GDELTEvent

log = logging.getLogger(__name__)

ConflictEvent = Union[ACLEDEvent, GDELTEvent]

DEFAULT_TIME_WINDOW_H: Final[float] = 24.0
DEFAULT_SPACE_WINDOW_M: Final[float] = 2_000.0

_EARTH_RADIUS_M: Final[float] = 6_371_000.0


@dataclass(frozen=True)
class CorroborationMatch:
    """A conflict event matched to a fire candidate with source metadata."""

    event: ConflictEvent
    source_type: str  # "acled" or "gdelt"
    distance_m: float


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


def _event_coords(event: ConflictEvent) -> tuple[float, float]:
    """Extract (latitude, longitude) from either event type."""
    return event.latitude, event.longitude


def _event_detected_at(event: ConflictEvent):
    """Extract the detected_at timestamp from either event type."""
    return event.detected_at


def find_corroboration(
    candidate: CandidateFireEvent,
    conflict_events: list[ConflictEvent],
    *,
    time_window_h: float = DEFAULT_TIME_WINDOW_H,
    space_window_m: float = DEFAULT_SPACE_WINDOW_M,
) -> list[CorroborationMatch]:
    """Return conflict events that spatially and temporally overlap a candidate.

    Works with both ACLEDEvent and GDELTEvent records. The returned matches
    include a ``source_type`` field ("acled" or "gdelt") so that
    ``confidence.py`` can distinguish strong (ACLED) from weak (GDELT)
    corroboration.

    Parameters
    ----------
    candidate : CandidateFireEvent
        The clustered fire candidate to match against.
    conflict_events : list[ACLEDEvent | GDELTEvent]
        Pre-fetched conflict records to search.
    time_window_h : float
        Hours to extend the search window beyond the event day boundary.
    space_window_m : float
        Maximum haversine distance (metres) for a spatial match.

    Returns
    -------
    list[CorroborationMatch]
        All matching events sorted ascending by distance, with source_type.
    """
    extension = timedelta(hours=time_window_h)
    candidate_t = candidate.first_detected_at
    matches: list[tuple[float, CorroborationMatch]] = []

    for event in conflict_events:
        detected = _event_detected_at(event)
        event_start = detected - extension
        event_end = detected + timedelta(hours=24) + extension
        if not (event_start <= candidate_t <= event_end):
            continue

        lat, lon = _event_coords(event)
        dist_m = _haversine_m(
            candidate.centroid_lat, candidate.centroid_lon, lat, lon,
        )
        if dist_m <= space_window_m:
            source_type = "acled" if isinstance(event, ACLEDEvent) else "gdelt"
            matches.append((
                dist_m,
                CorroborationMatch(
                    event=event,
                    source_type=source_type,
                    distance_m=dist_m,
                ),
            ))

    matches.sort(key=lambda t: t[0])
    result = [m for _, m in matches]
    log.debug(
        "find_corroboration: candidate=%s matches=%d "
        "(time_window_h=%.0f space_window_m=%.0f)",
        candidate.id,
        len(result),
        time_window_h,
        space_window_m,
    )
    return result


def find_acled_corroboration(
    candidate: CandidateFireEvent,
    acled_events: list[ACLEDEvent],
    *,
    time_window_h: float = DEFAULT_TIME_WINDOW_H,
    space_window_m: float = DEFAULT_SPACE_WINDOW_M,
) -> list[ACLEDEvent]:
    """Backward-compatible wrapper that returns only ACLEDEvent objects.

    Delegates to ``find_corroboration`` and unwraps the matches.
    Existing callers (pipeline, tests) can continue using this function
    unchanged.
    """
    matches = find_corroboration(
        candidate,
        acled_events,  # type: ignore[arg-type]
        time_window_h=time_window_h,
        space_window_m=space_window_m,
    )
    return [m.event for m in matches]  # type: ignore[misc]
