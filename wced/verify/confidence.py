"""Confidence label assignment for fire candidates.

Combines three independent evidence streams — FIRMS persistence, Sentinel-2
optical classification, and conflict-event corroboration (ACLED or GDELT) —
into a single :class:`~wced.models.provenance.ConfidenceLabel`. The label
governs which editorial tier a ``FireEvent`` enters and propagates forward
into every emission estimate that cites this candidate.

Label hierarchy and evidence requirements (methodology/v1.0.pdf §4.3, Table 5):

  CONFIRMED  — FIRMS persistent (≥2 overpasses) + S2 confirms fire
               + ≥1 ACLED match within space/time window (or both ACLED + GDELT).
  VERIFIED   — FIRMS persistent + S2 confirms fire + GDELT match (no ACLED),
               OR FIRMS persistent + S2 fire (no conflict-event match at all).
  REPORTED   — FIRMS persistent + no optical confirmation (clouds blocked S2
               or no clear-sky scene within the search window).
  SUSPECTED  — FIRMS single-overpass, no other confirmation. May be flaring,
               sensor noise, or a genuine brief fire on the first pass.
  CLAIMED    — Only a state/news source claims an event; no satellite evidence
               exists. Rare in this pipeline (ingested via a future news-triage
               prompt) but must be handled to avoid blocking that path.

Corroboration source strength:
  - ACLED (human-reviewed) → strong corroboration, can reach CONFIRMED.
  - GDELT (machine-extracted) → weak corroboration, caps at VERIFIED.
    GDELT corroboration can never push an event above REPORTED on its own
    because GDELT is machine-extracted, not human-reviewed.
  - Both ACLED + GDELT → CONFIRMED (ACLED dominates).

Edge case — conflict-event-only: a conflict record with no FIRMS hotspot is
NOT auto-confirmed. These candidates are flagged SUSPECTED and routed for
editorial review.

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
from wced.verify.corroboration import CorroborationMatch
from wced.verify.sentinel2_check import FireLabel, VerificationStatus, VerifiedCandidate

log = logging.getLogger(__name__)

_METHOD: Final[str] = "confidence_assignment_v1.1"

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
    corroboration_matches: list[CorroborationMatch] | None = None,
    store: ProvenanceStore,
    produced_at: datetime | None = None,
) -> ConfidenceLabel:
    """Assign a confidence label from the three evidence streams.

    Parameters
    ----------
    candidate : CandidateFireEvent
        The clustered FIRMS candidate being evaluated.
    s2_result : VerifiedCandidate or None
        Result of the Sentinel-2 optical check.
    acled_matches : list[ACLEDEvent]
        Pre-filtered ACLED matches (backward compatibility). May be empty.
    corroboration_matches : list[CorroborationMatch] or None
        Source-typed corroboration matches from ``find_corroboration``.
        When provided, these take precedence over ``acled_matches`` for
        determining corroboration source strength.
    store : ProvenanceStore
        Receives the ProvenanceRecord.
    produced_at : datetime or None
        Wall-clock time for the ProvenanceRecord. Defaults to UTC now.

    Returns
    -------
    ConfidenceLabel
    """
    ts = produced_at or datetime.now(tz=UTC)
    persistent = _is_persistent(candidate)
    s2_fire = _s2_confirms_fire(s2_result)

    # Determine corroboration strength from typed matches if available,
    # otherwise fall back to acled_matches (backward compat).
    has_acled = False
    has_gdelt = False

    if corroboration_matches is not None:
        for m in corroboration_matches:
            if m.source_type == "acled":
                has_acled = True
            elif m.source_type == "gdelt":
                has_gdelt = True
    else:
        has_acled = len(acled_matches) > 0

    has_any_corroboration = has_acled or has_gdelt

    # --- decision table (methodology/v1.0.pdf §4.3, Table 5) ---
    # Extended with corroboration source distinction:
    #   ACLED match → can reach CONFIRMED
    #   GDELT match only → caps at VERIFIED (one tier lower)
    #   Both → CONFIRMED (ACLED dominates)

    if persistent and s2_fire and has_acled:
        label = ConfidenceLabel.CONFIRMED
    elif persistent and s2_fire and has_gdelt:
        # GDELT corroboration caps at VERIFIED — cannot reach CONFIRMED.
        label = ConfidenceLabel.VERIFIED
    elif persistent and s2_fire:
        label = ConfidenceLabel.VERIFIED
    elif persistent and not s2_fire:
        label = ConfidenceLabel.REPORTED
    elif not persistent and has_any_corroboration:
        log.warning(
            "assign_confidence: candidate=%s has corroboration match(es) but "
            "only %d FIRMS overpass(es); not auto-promoting. Flag for "
            "editorial review (possible near-miss or non-incendiary strike).",
            candidate.id,
            candidate.n_overpasses,
        )
        label = ConfidenceLabel.SUSPECTED
    else:
        label = ConfidenceLabel.SUSPECTED

    # Build event IDs list for provenance
    corr_event_ids: list[str] = []
    corr_source_types: list[str] = []
    if corroboration_matches is not None:
        for m in corroboration_matches:
            corr_source_types.append(m.source_type)
            if isinstance(m.event, ACLEDEvent):
                corr_event_ids.append(m.event.event_id_cnty)
            else:
                corr_event_ids.append(m.event.event_id)
    else:
        corr_event_ids = [ev.event_id_cnty for ev in acled_matches]
        corr_source_types = ["acled"] * len(acled_matches)

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
            "n_corroboration_matches": len(corr_event_ids),
            "corroboration_event_ids": corr_event_ids,
            "corroboration_source_types": corr_source_types,
            "has_acled": has_acled,
            "has_gdelt": has_gdelt,
            "assigned_label": label.value,
        },
        produced_at=ts,
        confidence_label=label,
    )
    store.record_provenance(rec)
    log.info(
        "assign_confidence: candidate=%s → %s "
        "(persistent=%s s2_fire=%s acled=%s gdelt=%s)",
        candidate.id,
        label.value,
        persistent,
        s2_fire,
        has_acled,
        has_gdelt,
    )
    return label
