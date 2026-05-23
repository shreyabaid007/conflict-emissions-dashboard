"""Confidence label assignment for fire candidates.

Combines three independent evidence streams — FIRMS persistence, Sentinel-2
optical classification, and ACLED armed-event corroboration — into a single
:class:`~wced.models.provenance.ConfidenceLabel`. The label governs which
editorial tier a ``FireEvent`` enters and propagates forward into every
emission estimate that cites this candidate.

Label hierarchy and evidence requirements (methodology/v1.0.pdf §4.3, Table 5):

  CONFIRMED  — FIRMS persistent (≥2 overpasses) + S2 confirms fire
               + ≥1 ACLED match within space/time window.
  VERIFIED   — FIRMS persistent + S2 confirms fire. No ACLED yet (could be
               cloud-free confirmation without a documented conflict event, or
               ACLED data lag).
  REPORTED   — FIRMS persistent + no optical confirmation (clouds blocked S2
               or no clear-sky scene within the search window).
  SUSPECTED  — FIRMS single-overpass, no other confirmation. May be flaring,
               sensor noise, or a genuine brief fire on the first pass.
  CLAIMED    — Only a state/news source claims an event; no satellite evidence
               exists. Rare in this pipeline (ingested via a future news-triage
               prompt) but must be handled to avoid blocking that path.

Edge case — ACLED-only: an ACLED record of a strike with no FIRMS hotspot is
NOT auto-confirmed. The strike may have been a near-miss, or the fire may have
been too brief or too small to register above FIRMS detection thresholds. These
candidates are flagged SUSPECTED and routed for editorial review rather than
being promoted automatically.

Methodology reference: methodology/v1.0.pdf §4.3 — "Verification and
Confidence Labels".
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from wced.detect.hotspot import CandidateFireEvent
from wced.ingest.acled import ACLEDEvent
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord
from wced.provenance.store import ProvenanceStore
from wced.verify.sentinel2_check import FireLabel, VerificationStatus, VerifiedCandidate

log = logging.getLogger(__name__)

_METHOD: Final[str] = "confidence_assignment_v1.0"

# Persistence threshold from methodology/v1.0.pdf §3.4.
_PERSISTENT_MIN_OVERPASSES: Final[int] = 2


def _is_persistent(candidate: CandidateFireEvent) -> bool:
    return candidate.n_overpasses >= _PERSISTENT_MIN_OVERPASSES


def _s2_confirms_fire(verified: VerifiedCandidate | None) -> bool:
    """True iff optical verification returned a CONFIRMED_FIRE label."""
    if verified is None:
        return False
    if verified.classification is None:
        return False
    return verified.classification.label is FireLabel.CONFIRMED_FIRE


def _s2_was_attempted(verified: VerifiedCandidate | None) -> bool:
    """True iff an S2 scene was found and the classifier ran (even if ambiguous)."""
    if verified is None:
        return False
    return verified.status not in (
        VerificationStatus.AWAITING_OPTICAL_CHECK,
    )


def assign_confidence(
    candidate: CandidateFireEvent,
    s2_result: VerifiedCandidate | None,
    acled_matches: list[ACLEDEvent],
    *,
    store: ProvenanceStore,
    produced_at: datetime | None = None,
) -> ConfidenceLabel:
    """Assign a confidence label from the three evidence streams.

    Parameters
    ----------
    candidate : CandidateFireEvent
        The clustered FIRMS candidate being evaluated.
    s2_result : VerifiedCandidate or None
        Result of the Sentinel-2 optical check. Pass None when the check has
        not been attempted (e.g. during rapid triage before S2 scenes are
        available). Distinct from a result with status AWAITING_OPTICAL_CHECK,
        though both produce the same downstream label.
    acled_matches : list[ACLEDEvent]
        Pre-filtered list from ``find_acled_corroboration``. May be empty.
    store : ProvenanceStore
        Receives the ProvenanceRecord documenting this assignment so the label
        is always auditable.
    produced_at : datetime or None
        Wall-clock time for the ProvenanceRecord. Defaults to UTC now.

    Returns
    -------
    ConfidenceLabel
        The weakest-justified label that the evidence supports. The label is
        simultaneously recorded as a ProvenanceRecord in ``store``.

    Notes
    -----
    The function intentionally does NOT auto-promote an ACLED-only match.
    An armed event near a facility without a FIRMS hotspot is suspicious but
    not confirmatory — it could be a near-miss, a non-incendiary strike, or a
    mis-geocoded ACLED record. ``assign_confidence`` returns SUSPECTED in that
    case and logs a warning so the editorial queue can review it.
    """
    ts = produced_at or datetime.now(tz=UTC)
    persistent = _is_persistent(candidate)
    s2_fire = _s2_confirms_fire(s2_result)
    has_acled = len(acled_matches) > 0

    # --- decision table (methodology/v1.0.pdf §4.3, Table 5) ---

    if persistent and s2_fire and has_acled:
        label = ConfidenceLabel.CONFIRMED
    elif persistent and s2_fire:
        label = ConfidenceLabel.VERIFIED
    elif persistent and not s2_fire:
        # Optical check attempted + rejected/ambiguous vs. clouds/no scene:
        # both produce REPORTED — we have ≥2 FIRMS passes, just no optics.
        label = ConfidenceLabel.REPORTED
    elif not persistent and has_acled:
        # ACLED-only promotion is intentionally blocked — route for review.
        log.warning(
            "assign_confidence: candidate=%s has ACLED match(es) but only "
            "%d FIRMS overpass(es); not auto-promoting. Flag for editorial "
            "review (possible near-miss or non-incendiary strike).",
            candidate.id,
            candidate.n_overpasses,
        )
        label = ConfidenceLabel.SUSPECTED
    else:
        # Single-overpass, no ACLED, no S2 fire: weakest non-claimed label.
        label = ConfidenceLabel.SUSPECTED

    rec = ProvenanceRecord(
        produced_by="wced.verify.confidence",
        inputs=[candidate.provenance_id],
        method=_METHOD,
        parameters={
            "n_overpasses": candidate.n_overpasses,
            "persistent": persistent,
            "s2_status": (s2_result.status.value if s2_result is not None else None),
            "s2_label": (
                s2_result.classification.label.value
                if s2_result is not None and s2_result.classification is not None
                else None
            ),
            "s2_confirms_fire": s2_fire,
            "n_acled_matches": len(acled_matches),
            "acled_event_ids": [ev.event_id_cnty for ev in acled_matches],
            "assigned_label": label.value,
        },
        produced_at=ts,
        confidence_label=label,
    )
    store.record_provenance(rec)
    log.info(
        "assign_confidence: candidate=%s → %s "
        "(persistent=%s s2_fire=%s acled_matches=%d)",
        candidate.id,
        label.value,
        persistent,
        s2_fire,
        len(acled_matches),
    )
    return label
