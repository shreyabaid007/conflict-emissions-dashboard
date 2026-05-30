"""Pydantic v2 response models for the WCED public API."""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


METHODOLOGY_VERSION = "1.0.5"
DATA_LICENSE = "CC-BY 4.0"
ATTRIBUTION = "Data: NASA FIRMS, ESA Copernicus, ACLED. Analysis: WCED v1.0.5"


class _Envelope(BaseModel):
    """Fields included on every API response."""

    model_config = ConfigDict(from_attributes=True)

    methodology_version: str = METHODOLOGY_VERSION
    generated_at: datetime
    data_license: str = DATA_LICENSE
    attribution: str = ATTRIBUTION


class PaginationMeta(BaseModel):
    total: int
    page: int
    per_page: int
    pages: int


class EmissionEstimateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    methodology_version: str
    method: str
    p5: float
    p50: float
    p95: float
    units: str
    created_at: datetime


class EventSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    facility_id: UUID
    detected_at: datetime
    last_seen_at: datetime
    peak_frp_mw: float
    total_frp_integral_mj: float | None
    detection_source: str
    confidence_label: str
    status: str
    notes: str | None
    estimate: EmissionEstimateOut | None = None


class EventListResponse(_Envelope):
    data: list[EventSummary]
    pagination: PaginationMeta


class ProvenanceNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_type: str
    id: UUID
    detail: dict


class ProvenanceResponse(_Envelope):
    event_id: UUID
    chain: list[ProvenanceNodeOut]
    rendered: str


class ProvenanceChainResponse(_Envelope):
    """Standalone provenance chain for any provenance/source id.

    Unlike ``ProvenanceResponse`` this is not scoped to an event — it makes
    every number on the dashboard click-through-auditable via
    ``GET /v1/provenance/{id}`` (v2 §6, gap C.8).
    """

    provenance_id: UUID
    chain: list[ProvenanceNodeOut]
    rendered: str


class RevisionEntry(BaseModel):
    """One row of the append-only publication log, surfaced publicly.

    Retractions and restatements are shown, never silently deleted
    (CLAUDE.md §"Editorial Workflow"). ``public_note`` carries the public
    "under review" flag set when an estimate is auto-retracted by
    anomaly-watch.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_type: str
    target_id: UUID
    from_state: str
    to_state: str
    action: str
    actor: str
    reason: str | None = None
    public_note: str | None = None
    methodology_version: str | None = None
    created_at: datetime


class RevisionLogResponse(_Envelope):
    data: list[RevisionEntry]
    pagination: PaginationMeta


class EventDetailResponse(_Envelope):
    data: EventSummary
    estimates: list[EmissionEstimateOut]


class DamageAssessmentResponse(_Envelope):
    event_id: UUID
    data: dict | None


class FacilitySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    facility_type: str
    country: str
    capacity_barrels: float | None
    operator: str | None
    latitude: float | None = None
    longitude: float | None = None


class FacilityDetailResponse(_Envelope):
    data: FacilitySummary
    geometry_wkt: str
    event_count: int
    total_p50_tco2e: float


class FacilityListResponse(_Envelope):
    data: list[FacilitySummary]
    pagination: PaginationMeta


class DailyPoint(BaseModel):
    date: date
    p5: float
    p50: float
    p95: float


class TimeseriesResponse(_Envelope):
    data: list[DailyPoint]


class AggregateRow(BaseModel):
    key: str
    p5: float
    p50: float
    p95: float
    event_count: int


class AggregatesResponse(_Envelope):
    data: list[AggregateRow]


class MethodologyResponse(_Envelope):
    version_id: str
    released_at: datetime
    pdf_url: str


class ChangelogEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_id: str | None = None
    event_id: UUID | None = None
    change_type: str
    detail: str
    occurred_at: datetime


class ChangelogResponse(_Envelope):
    entries: list[ChangelogEntry]


class MetaResponse(BaseModel):
    methodology_version: str = METHODOLOGY_VERSION
    last_data_update: str | None = None
    event_count: int
    facility_count: int


class HeadlineResponse(_Envelope):
    total_p5: float
    total_p50: float
    total_p95: float
    confirmed_event_count: int
    facility_count: int


class HealthResponse(BaseModel):
    status: str
    version: str = "0.1.0"
    database: str
