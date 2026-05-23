"""Oil/fuel fire emission category.

Wraps the existing detect → verify → quantify pipeline for oil and fuel
infrastructure fires into the ``EmissionCategory`` protocol. All numeric
logic delegates to the original modules — this layer adds only the uniform
category interface.

Context keys consumed by this category:

  detect():
    - "firms_detections" : list[dict]     (VIIRS + MODIS rows from ingest)
    - "facilities"       : list[Facility]
    - "provenance_store" : ProvenanceStore

  verify():
    - "s2_chips"         : dict[str, S2ChipResult | None]
    - "conflict_events"  : list[ConflictEvent]
    - "verified_candidates" : dict[str, VerifiedCandidate]
    - "provenance_store" : ProvenanceStore

  quantify():
    (uses only the DetectionEvent.data and VerificationResult; no ctx needed)
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Final
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import numpy as np

from wced.categories.base import (
    DetectionEvent,
    EmissionCategory,
    SourceSpec,
    VerificationResult,
)
from wced.detect.facility_match import build_facility_tree, match_to_facility_with_tree
from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection, hotspots_to_candidates
from wced.models.event import DetectionSource, EventStatus, FireEvent
from wced.models.facility import Facility
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord
from wced.provenance.store import InMemoryProvenanceStore, ProvenanceStore
from wced.quantify.distribution import Distribution
from wced.quantify.factors import load_factors, load_parameter_distributions
from wced.quantify.frp import compute_frp_emissions
from wced.verify.confidence import assign_confidence
from wced.verify.corroboration import CorroborationMatch, find_corroboration
from wced.verify.sentinel2_check import VerificationStatus, VerifiedCandidate

log = logging.getLogger(__name__)

CATEGORY_ID: Final[str] = "oil_fuel_fire"
METHODOLOGY_VERSION: Final[str] = "1.1.0"


class OilFuelFireCategory:
    """Oil/fuel infrastructure fire emissions (FRP method).

    Implements the EmissionCategory protocol by delegating to the existing
    detect, verify, and quantify modules. No numeric logic is duplicated;
    this class is a thin adapter.
    """

    @property
    def id(self) -> str:
        return CATEGORY_ID

    @property
    def methodology_version(self) -> str:
        return METHODOLOGY_VERSION

    def required_sources(self) -> list[SourceSpec]:
        return [
            SourceSpec(
                name="firms_viirs",
                description="NASA FIRMS VIIRS thermal anomaly detections",
            ),
            SourceSpec(
                name="firms_modis",
                description="NASA FIRMS MODIS thermal anomaly detections",
                required=False,
            ),
            SourceSpec(
                name="gdelt",
                description="GDELT conflict event corroboration",
                required=False,
            ),
            SourceSpec(
                name="sentinel2",
                description="Sentinel-2 L2A optical chips for fire classification",
                required=False,
            ),
        ]

    def detect(self, ctx: dict[str, Any]) -> list[DetectionEvent]:
        """Cluster FIRMS detections and match to facilities.

        Delegates to ``hotspots_to_candidates`` and ``match_to_facility_with_tree``.
        Each resulting MatchedCandidate is wrapped in a DetectionEvent.

        Required context keys:
          - "firms_detections": list[FIRMSDetection]
          - "facilities": list[Facility]
          - "provenance_store": ProvenanceStore (optional; created if absent)
        """
        detections: list[FIRMSDetection] = ctx.get("firms_detections", [])
        facilities: list[Facility] = ctx.get("facilities", [])
        store: ProvenanceStore = ctx.get("provenance_store", InMemoryProvenanceStore())

        if not detections:
            return []

        candidates = hotspots_to_candidates(detections, store=store)

        tree, fac_list = (
            build_facility_tree(facilities) if facilities else (None, [])
        )

        events: list[DetectionEvent] = []
        for candidate in candidates:
            if fac_list and tree is not None:
                facility, dist_m = match_to_facility_with_tree(
                    candidate, tree, fac_list, store=store,
                )
            else:
                facility, dist_m = None, float("inf")

            cand_hash = hashlib.sha256(
                "|".join(sorted(str(h.id) for h in candidate.hotspots)).encode()
            ).hexdigest()

            events.append(DetectionEvent(
                event_id=str(candidate.id),
                category_id=self.id,
                data={
                    "candidate": candidate,
                    "facility": facility,
                    "match_distance_m": dist_m,
                    "detection_hash": cand_hash,
                },
            ))

        log.info(
            "oil_fuel_fire.detect: %d detections -> %d candidates",
            len(detections),
            len(events),
        )
        return events

    def verify(
        self, event: DetectionEvent, ctx: dict[str, Any],
    ) -> VerificationResult:
        """Verify a single detection using S2 classification and corroboration.

        Delegates to ``assign_confidence`` from ``wced.verify.confidence``.

        Context keys used:
          - "verified_candidates": dict[str, VerifiedCandidate]
          - "corroboration_matches": dict[str, list[CorroborationMatch]]
          - "provenance_store": ProvenanceStore (optional)
        """
        candidate: CandidateFireEvent = event.data["candidate"]
        store: ProvenanceStore = ctx.get("provenance_store", InMemoryProvenanceStore())

        verified_candidates: dict[str, VerifiedCandidate] = ctx.get(
            "verified_candidates", {},
        )
        corroboration_map: dict[str, list[CorroborationMatch]] = ctx.get(
            "corroboration_matches", {},
        )

        s2_result = verified_candidates.get(event.event_id)
        corr_matches = corroboration_map.get(event.event_id, [])

        label = assign_confidence(
            candidate,
            s2_result,
            [],
            corroboration_matches=corr_matches,
            store=store,
        )

        return VerificationResult(
            event_id=event.event_id,
            verified=label in (ConfidenceLabel.CONFIRMED, ConfidenceLabel.VERIFIED),
            confidence_label=label.value,
            data={
                "confidence_label_enum": label,
                "s2_result": s2_result,
                "corroboration_matches": corr_matches,
                "candidate": candidate,
                "facility": event.data.get("facility"),
            },
        )

    def quantify(
        self,
        event: DetectionEvent,
        verification: VerificationResult,
    ) -> Distribution:
        """Produce an FRP-based emission estimate for a verified event.

        Delegates to ``compute_frp_emissions`` from ``wced.quantify.frp``.
        Returns a zero distribution for events without a facility match or
        without sufficient FRP data.
        """
        candidate: CandidateFireEvent = event.data["candidate"]
        facility: Facility | None = event.data.get("facility")
        label: ConfidenceLabel = verification.data["confidence_label_enum"]

        if facility is None:
            return Distribution.from_samples(
                np.zeros(100),
                units="tCO2e",
                methodology_version=self.methodology_version,
                provenance_id=uuid4(),
            )

        now = datetime.now(UTC)
        fire_event = FireEvent(
            facility_id=facility.id,
            detected_at=candidate.first_detected_at,
            last_seen_at=candidate.last_detected_at,
            peak_frp_mw=candidate.peak_frp_mw,
            total_frp_integral_mj=_estimate_frp_integral(candidate),
            detection_source=candidate.hotspots[0].detection_source,
            confidence_label=label,
            status=EventStatus.PENDING_REVIEW,
            provenance_id=candidate.provenance_id,
            created_at=now,
            updated_at=now,
        )

        if fire_event.total_frp_integral_mj is None:
            return Distribution.from_samples(
                np.zeros(100),
                units="tCO2e",
                methodology_version=self.methodology_version,
                provenance_id=uuid4(),
            )

        factors = load_factors()
        return compute_frp_emissions(
            fire_event,
            factors,
            methodology_version=self.methodology_version,
        )


def _estimate_frp_integral(candidate: CandidateFireEvent) -> float | None:
    """Trapezoidal FRP integral in MJ from candidate hotspots.

    Returns None for single-overpass candidates (methodology v1.0 §3.2
    forbids quantifying these).
    """
    if candidate.n_overpasses < 2:
        return None

    hotspots = sorted(candidate.hotspots, key=lambda h: h.detected_at)
    total_mj = 0.0
    for i in range(1, len(hotspots)):
        dt_s = (hotspots[i].detected_at - hotspots[i - 1].detected_at).total_seconds()
        avg_mw = (hotspots[i].frp_mw + hotspots[i - 1].frp_mw) / 2.0
        total_mj += avg_mw * dt_s
    return total_mj
