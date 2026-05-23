"""Fire event persistence scoring.

A CandidateFireEvent is considered *persistent* if it was observed on at least
two separate overpasses with FRP exceeding 2× the facility background baseline
within a 24-hour sliding window. Single-overpass candidates are too likely to
be routine flaring or sensor noise; they are flagged SUSPECTED and held in
pending_review.

Methodology reference: methodology/v1.0.pdf §3.4 — "Persistence Criterion and
Single-Overpass Handling".
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from wced.detect.baseline import FacilityBaseline
from wced.detect.hotspot import CandidateFireEvent
from wced.models.event import EventStatus
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord
from wced.provenance.store import ProvenanceStore

log = logging.getLogger(__name__)

# Persistence thresholds — methodology/v1.0.pdf §3.4
MIN_QUALIFYING_OVERPASSES: Final[int] = 2
FRP_BASELINE_MULTIPLIER: Final[float] = 2.0
PERSISTENCE_WINDOW_H: Final[float] = 24.0


def is_persistent_event(
    candidate: CandidateFireEvent,
    baseline: FacilityBaseline,
    *,
    min_overpasses: int = MIN_QUALIFYING_OVERPASSES,
    frp_multiplier: float = FRP_BASELINE_MULTIPLIER,
    temporal_window_h: float = PERSISTENCE_WINDOW_H,
    store: ProvenanceStore,
    produced_at: datetime | None = None,
) -> bool:
    """Determine whether a CandidateFireEvent meets the persistence criterion.

    A candidate is persistent if at least *min_overpasses* separate acquisition
    timestamps show FRP > *frp_multiplier* × the facility baseline, and those
    qualifying acquisitions all fall within a *temporal_window_h*-hour window.

    Candidates that do not qualify are classified SUSPECTED and should be
    placed in EventStatus.PENDING_REVIEW rather than published.

    A ProvenanceRecord is emitted to *store* for every call, capturing the
    threshold parameters and qualifying-overpass count so the decision is
    always auditable.

    Parameters
    ----------
    candidate : CandidateFireEvent
        The clustered fire candidate to evaluate.
    baseline : FacilityBaseline
        Pre-computed background FRP for the attributed facility. If
        is_fallback=True the threshold is still applied, but the result
        is inherently uncertain due to the unknown background.
    min_overpasses : int
        Number of qualifying overpasses required. Default 2.
    frp_multiplier : float
        FRP must exceed this multiple of baseline_frp_mw to qualify.
        Default 2× per methodology §3.4.
    temporal_window_h : float
        All qualifying overpasses must fit within this sliding window (hours).
        Default 24 h.
    store : ProvenanceStore
        Receives one ProvenanceRecord documenting this persistence decision.
    produced_at : datetime or None
        Wall-clock time for the ProvenanceRecord. Defaults to UTC now.

    Returns
    -------
    bool
        True if the candidate satisfies the persistence criterion.
        False otherwise (single-overpass or below-threshold candidates).
    """
    ts = produced_at or datetime.now(tz=UTC)
    threshold_frp = baseline.baseline_frp_mw * frp_multiplier

    # Aggregate hotspot FRP by overpass timestamp (take the maximum across
    # co-located pixels within a single pass — peak FRP is the relevant signal).
    overpass_peak: dict[datetime, float] = {}
    for hotspot in candidate.hotspots:
        t = hotspot.detected_at
        overpass_peak[t] = max(overpass_peak.get(t, 0.0), hotspot.frp_mw)

    qualifying_times = sorted(t for t, frp in overpass_peak.items() if frp > threshold_frp)
    n_qualifying = len(qualifying_times)

    window = timedelta(hours=temporal_window_h)
    persistent = False
    if n_qualifying >= min_overpasses:
        # Sliding window check: find the earliest window of size ≥ min_overpasses
        # where the span ≤ temporal_window_h.
        for i in range(n_qualifying - min_overpasses + 1):
            span = qualifying_times[i + min_overpasses - 1] - qualifying_times[i]
            if span <= window:
                persistent = True
                break

    confidence = (
        ConfidenceLabel.REPORTED if persistent else ConfidenceLabel.SUSPECTED
    )
    notes: str | None = None
    if not persistent:
        if n_qualifying == 0:
            notes = (
                f"No overpasses exceeded {frp_multiplier}× baseline "
                f"({threshold_frp:.1f} MW); holding as SUSPECTED."
            )
        elif n_qualifying < min_overpasses:
            notes = (
                f"Only {n_qualifying}/{min_overpasses} qualifying overpasses "
                f"(>{threshold_frp:.1f} MW); holding as SUSPECTED."
            )
        else:
            notes = (
                f"{n_qualifying} qualifying overpasses but none within a "
                f"{temporal_window_h:.0f} h window; holding as SUSPECTED."
            )

    rec = ProvenanceRecord(
        produced_by="wced.detect.persistence",
        inputs=[candidate.provenance_id, baseline.provenance_id],
        method="persistence_criterion_v1.0",
        parameters={
            "min_overpasses": min_overpasses,
            "frp_multiplier": frp_multiplier,
            "temporal_window_h": temporal_window_h,
            "baseline_frp_mw": baseline.baseline_frp_mw,
            "baseline_is_fallback": baseline.is_fallback,
            "threshold_frp_mw": threshold_frp,
            "n_total_overpasses": len(overpass_peak),
            "n_qualifying_overpasses": n_qualifying,
            "persistent": persistent,
        },
        produced_at=ts,
        confidence_label=confidence,
        notes=notes,
    )
    store.record_provenance(rec)

    log.info(
        "is_persistent_event: candidate %s → %s "
        "(qualifying_overpasses=%d/%d, threshold=%.1f MW)",
        candidate.id,
        "PERSISTENT" if persistent else "SUSPECTED",
        n_qualifying,
        min_overpasses,
        threshold_frp,
    )
    return persistent


def candidate_status(
    candidate: CandidateFireEvent,
    baseline: FacilityBaseline,
    *,
    min_overpasses: int = MIN_QUALIFYING_OVERPASSES,
    frp_multiplier: float = FRP_BASELINE_MULTIPLIER,
    temporal_window_h: float = PERSISTENCE_WINDOW_H,
    store: ProvenanceStore,
    produced_at: datetime | None = None,
) -> EventStatus:
    """Return the EventStatus appropriate for this candidate.

    Persistent events enter PENDING_REVIEW (eligible for editorial publishing).
    Non-persistent singleton candidates also enter PENDING_REVIEW but with a
    SUSPECTED confidence label on their ProvenanceRecord — they require manual
    review before publication.

    Parameters
    ----------
    candidate : CandidateFireEvent
        The candidate to evaluate.
    baseline : FacilityBaseline
        Background FRP baseline for the attributed facility.
    min_overpasses : int
        Forwarded to is_persistent_event.
    frp_multiplier : float
        Forwarded to is_persistent_event.
    temporal_window_h : float
        Forwarded to is_persistent_event.
    store : ProvenanceStore
        Forwarded to is_persistent_event.
    produced_at : datetime or None
        Forwarded to is_persistent_event.

    Returns
    -------
    EventStatus
        Always EventStatus.PENDING_REVIEW — both persistent and singleton
        candidates enter the editorial queue; only their confidence label
        differs.
    """
    is_persistent_event(
        candidate,
        baseline,
        min_overpasses=min_overpasses,
        frp_multiplier=frp_multiplier,
        temporal_window_h=temporal_window_h,
        store=store,
        produced_at=produced_at,
    )
    # All new candidates enter PENDING_REVIEW; the persistence ProvenanceRecord
    # in the store carries the confidence label that distinguishes them.
    return EventStatus.PENDING_REVIEW
