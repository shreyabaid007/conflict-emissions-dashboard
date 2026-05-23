"""Fire event data models.

A FireEvent is a single thermal anomaly attributed to a registered Facility,
observed across one or more satellite overpasses. Persistent events that
survive ≥2 overpasses are the only ones eligible for emissions quantification
under methodology v1.0 — single-overpass detections are too easily confused
with flaring or routine industrial heat.

Methodology reference: methodology/v1.0.pdf §3 — "Fire Event Detection and
Persistence Criterion".
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

# Re-exported so callers can import a single ConfidenceLabel symbol from the
# events module without reaching into provenance. The enum is defined in
# provenance.py; treating it as canonical there keeps a single source of truth.
from wced.models.provenance import ConfidenceLabel

__all__ = [
    "ConfidenceLabel",
    "DetectionSource",
    "EventStatus",
    "FireEvent",
]


class DetectionSource(str, enum.Enum):
    """Satellite or sensor system that originally detected the fire.

    FIRMS distributes both VIIRS (375 m, ~12 h revisit) and MODIS (1 km,
    ~6 h revisit, two platforms) thermal anomaly products. S2 means
    Sentinel-2 SWIR-based detection. GEOSTATIONARY covers GOES-R / Himawari
    style hourly-cadence sensors.
    """

    FIRMS_VIIRS = "FIRMS_VIIRS"
    FIRMS_MODIS = "FIRMS_MODIS"
    S2 = "S2"
    GEOSTATIONARY = "GEOSTATIONARY"


class EventStatus(str, enum.Enum):
    """Editorial state of a FireEvent.

    Events enter as PENDING_REVIEW and move through the editorial workflow.
    Transitions to RETRACTED produce a public changelog entry — see
    CLAUDE.md ``Editorial Workflow``.
    """

    PENDING_REVIEW = "PENDING_REVIEW"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    RETRACTED = "RETRACTED"


class FireEvent(BaseModel):
    """A persistent thermal anomaly attached to a Facility.

    Parameters
    ----------
    id : UUID
        Stable identifier for this fire event.
    facility_id : UUID
        ID of the Facility this event is attributed to. Attribution is
        performed upstream of construction; FireEvent itself does not
        re-check spatial containment.
    detected_at : AwareDatetime
        Timestamp of the first satellite overpass that flagged this fire (UTC).
    last_seen_at : AwareDatetime
        Timestamp of the most recent overpass that still showed the fire
        (UTC). Must be >= detected_at.
    peak_frp_mw : float
        Peak Fire Radiative Power observed across all overpasses, in MW.
    total_frp_integral_mj : float or None
        FRP integrated across the event's duration, in MJ. None at first
        detection (single overpass — there is nothing to integrate yet);
        populated by ``wced.detect.persistence`` once temporal grouping
        accumulates ≥2 overpasses. This is the quantity that propagates
        into combustion CO2 estimation.
    detection_source : DetectionSource
        Sensor that produced the underlying detections.
    confidence_label : ConfidenceLabel
        Evidential confidence in the detection. Should match the terminal
        ProvenanceRecord's label.
    status : EventStatus
        Editorial state. New events start as PENDING_REVIEW.
    provenance_id : UUID
        ID of the ProvenanceRecord that produced this event's quantities.
    created_at : AwareDatetime
        When this row was written to the database.
    updated_at : AwareDatetime
        When this row was last modified (status transitions, corrections).
    notes : str or None
        Free-text annotation, e.g. "cloud-obscured between overpasses 3-5".
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    facility_id: UUID
    detected_at: AwareDatetime
    last_seen_at: AwareDatetime
    peak_frp_mw: float = Field(ge=0.0)
    total_frp_integral_mj: float | None = Field(default=None, ge=0.0)
    detection_source: DetectionSource
    confidence_label: ConfidenceLabel
    status: EventStatus = EventStatus.PENDING_REVIEW
    provenance_id: UUID
    created_at: AwareDatetime
    updated_at: AwareDatetime
    notes: str | None = None

    @model_validator(mode="after")
    def _check_temporal_ordering(self) -> FireEvent:
        if self.last_seen_at < self.detected_at:
            raise ValueError(
                "last_seen_at must be >= detected_at "
                f"(got detected_at={self.detected_at.isoformat()}, "
                f"last_seen_at={self.last_seen_at.isoformat()})"
            )
        if self.updated_at < self.created_at:
            raise ValueError(
                "updated_at must be >= created_at "
                f"(got created_at={self.created_at.isoformat()}, "
                f"updated_at={self.updated_at.isoformat()})"
            )
        return self

    @property
    def duration_hours(self) -> float:
        """Event duration in hours (last_seen_at − detected_at)."""
        return (self.last_seen_at - self.detected_at).total_seconds() / 3600.0

    @property
    def is_persistent(self) -> bool:
        """True iff the fire was caught on ≥2 distinct overpasses.

        Operationally: ``last_seen_at`` strictly greater than ``detected_at``.
        A single-overpass detection has equal timestamps and is rejected by
        methodology v1.0 §3.2 as too easily confused with flaring or
        transient industrial heat.
        """
        return self.last_seen_at > self.detected_at

    def as_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict with JSON-friendly primitives.

        UUIDs become strings, datetimes ISO-format strings, enums their
        string values. Suitable for logging and API responses without
        further conversion.
        """
        return self.model_dump(mode="json")
