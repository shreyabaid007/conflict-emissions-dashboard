"""Optical verification of fire candidates via Sentinel-2 chips.

For every ``CandidateFireEvent`` we attempt to find a low-cloud Sentinel-2 L2A
scene within ±72 h of the candidate's first detection, fetch a chip around the
candidate's centroid, and feed it to :func:`wced.ai.classify.classify_fire`.

When no usable scene exists (no cloud-free pass, or the chip download fails),
the candidate's verification status is set to ``AWAITING_OPTICAL_CHECK`` and
the pipeline continues — the candidate may be revisited on a later pass.

Methodology reference: methodology/v1.0.pdf §4.3 — "Optical Verification".
"""
from __future__ import annotations

import enum
import logging
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from wced.ai.classify import FireClassification, FireLabel, classify_fire
from wced.ai.claude_client import AnthropicClient
from wced.detect.hotspot import CandidateFireEvent
from wced.ingest.sentinel2 import Sentinel2Connector, Sentinel2Error
from wced.models.facility import Facility
from wced.provenance.store import ProvenanceStore

log = logging.getLogger(__name__)

# Optical search window — methodology/v1.0.pdf §4.3, Table 4.
S2_LOOKBACK_HOURS: Final[float] = 72.0
S2_LOOKAHEAD_HOURS: Final[float] = 72.0
S2_MAX_CLOUD_PCT: Final[float] = 30.0
S2_CHIP_HALF_WIDTH_DEG: Final[float] = 0.02  # ~2 km at the equator


class VerificationStatus(str, enum.Enum):
    """Outcome of the optical verification attempt."""

    VERIFIED = "VERIFIED"  # classifier returned CONFIRMED_FIRE
    REJECTED = "REJECTED"  # classifier returned GAS_FLARING or FALSE_POSITIVE
    AWAITING_OPTICAL_CHECK = "AWAITING_OPTICAL_CHECK"  # no usable S2 chip
    AMBIGUOUS = "AMBIGUOUS"  # classifier returned AMBIGUOUS


class VerifiedCandidate(BaseModel):
    """Wrapper combining a candidate with its optical-verification outcome."""

    model_config = ConfigDict(frozen=True)

    candidate: CandidateFireEvent
    status: VerificationStatus
    classification: FireClassification | None = None
    s2_item_id: str | None = None
    s2_cloud_cover: float | None = None
    notes: str | None = None
    provenance_ids: tuple[UUID, ...] = Field(default_factory=tuple)


def _bbox_around(lat: float, lon: float, half_deg: float) -> tuple[float, float, float, float]:
    return (lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg)


def _status_from_label(label: FireLabel) -> VerificationStatus:
    if label is FireLabel.CONFIRMED_FIRE:
        return VerificationStatus.VERIFIED
    if label is FireLabel.AMBIGUOUS:
        return VerificationStatus.AMBIGUOUS
    return VerificationStatus.REJECTED


def verify_candidate(
    candidate: CandidateFireEvent,
    facility: Facility,
    *,
    store: ProvenanceStore,
    s2_connector: Sentinel2Connector,
    ai_client: AnthropicClient | None = None,
    lookback_h: float = S2_LOOKBACK_HOURS,
    lookahead_h: float = S2_LOOKAHEAD_HOURS,
    max_cloud_pct: float = S2_MAX_CLOUD_PCT,
    chip_half_width_deg: float = S2_CHIP_HALF_WIDTH_DEG,
) -> VerifiedCandidate:
    """Run optical verification for a single candidate.

    Parameters
    ----------
    candidate : CandidateFireEvent
        Clustered fire candidate to verify.
    facility : Facility
        Facility the candidate has been attributed to.
    store : ProvenanceStore
        Receives the classification ProvenanceRecord and the S2 Source.
    s2_connector : Sentinel2Connector
        Sentinel-2 STAC connector (injected so tests can supply a stub).
    ai_client : AnthropicClient or None
        Used by the AI classification path. Constructed lazily inside
        ``classify_fire`` when None and the AI path is needed.
    lookback_h, lookahead_h : float
        Search-window half-widths around ``candidate.first_detected_at``.
    max_cloud_pct : float
        Maximum scene cloud cover (the connector falls back to best-available
        if no scene meets this).
    chip_half_width_deg : float
        Half-width of the chip bbox around the candidate centroid.

    Returns
    -------
    VerifiedCandidate
        Outcome wrapper. If no usable S2 scene was found the status is
        ``AWAITING_OPTICAL_CHECK`` and ``classification`` is None.
    """
    centre_t = candidate.first_detected_at
    window = (
        centre_t - timedelta(hours=lookback_h),
        centre_t + timedelta(hours=lookahead_h),
    )

    items = s2_connector.search_around(
        candidate.centroid_lat,
        candidate.centroid_lon,
        window,
        max_cloud_pct=max_cloud_pct,
    )
    if not items:
        log.info(
            "verify_candidate: no S2 scenes for candidate=%s window=%s/%s",
            candidate.id,
            window[0].isoformat(),
            window[1].isoformat(),
        )
        return VerifiedCandidate(
            candidate=candidate,
            status=VerificationStatus.AWAITING_OPTICAL_CHECK,
            notes="No Sentinel-2 scenes intersect the search window.",
        )

    best = items[0]
    cloud = float(best.properties.get("eo:cloud_cover", 100.0))
    bbox = _bbox_around(
        candidate.centroid_lat, candidate.centroid_lon, chip_half_width_deg
    )

    try:
        chip, source = s2_connector.fetch_chip(best, bbox)
    except Sentinel2Error as exc:
        log.warning(
            "verify_candidate: S2 chip fetch failed candidate=%s item=%s err=%s",
            candidate.id,
            best.id,
            exc,
        )
        return VerifiedCandidate(
            candidate=candidate,
            status=VerificationStatus.AWAITING_OPTICAL_CHECK,
            s2_item_id=best.id,
            s2_cloud_cover=cloud,
            notes=f"Sentinel-2 chip fetch failed: {exc}",
        )

    store.record_source(source)

    classification = classify_fire(
        chip,
        candidate,
        facility,
        store=store,
        client=ai_client,
    )
    status = _status_from_label(classification.label)
    log.info(
        "verify_candidate: candidate=%s status=%s label=%s confidence=%.2f "
        "s2_item=%s cloud=%.0f%%",
        candidate.id,
        status.value,
        classification.label.value,
        classification.confidence,
        best.id,
        cloud,
    )
    return VerifiedCandidate(
        candidate=candidate,
        status=status,
        classification=classification,
        s2_item_id=best.id,
        s2_cloud_cover=cloud,
        provenance_ids=(source.id, classification.provenance_id),
    )


def verify_candidates(
    pairs: list[tuple[CandidateFireEvent, Facility]],
    *,
    store: ProvenanceStore,
    s2_connector: Sentinel2Connector,
    ai_client: AnthropicClient | None = None,
) -> list[VerifiedCandidate]:
    """Verify a batch of (candidate, facility) pairs.

    Pre-pairing keeps this module free of facility-attribution logic; the
    caller is expected to have run ``wced.detect.facility_match`` first.
    """
    return [
        verify_candidate(
            cand,
            fac,
            store=store,
            s2_connector=s2_connector,
            ai_client=ai_client,
        )
        for cand, fac in pairs
    ]
