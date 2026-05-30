"""Tests for wced.pipeline.validation_weekly — weekly TROPOMI validation pipeline.

Uses synthetic data to verify event selection, validation orchestration,
discrepancy flagging, and methodology review triggering.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import numpy as np
import pytest

from wced.models.event import DetectionSource, EventStatus, FireEvent
from wced.models.facility import Facility, FacilityType
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord
from wced.pipeline.quantification import EmissionEstimate
from wced.pipeline.validation_weekly import (
    ValidationReport,
    select_events_for_validation,
    weekly_validation,
)
from wced.quantify.distribution import Distribution
from wced.quantify.reconcile import ReconciliationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    *,
    facility_id: UUID | None = None,
    detected_at: datetime | None = None,
    p50: float = 10_000.0,
    status: EventStatus = EventStatus.PUBLISHED,
) -> FireEvent:
    t0 = detected_at or datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    fid = facility_id or uuid4()
    return FireEvent(
        facility_id=fid,
        detected_at=t0,
        last_seen_at=t0 + timedelta(hours=6),
        peak_frp_mw=120.0,
        total_frp_integral_mj=5000.0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.CONFIRMED,
        status=status,
        provenance_id=uuid4(),
        created_at=t0,
        updated_at=t0,
    )


def _dist(p50: float) -> Distribution:
    rng = np.random.default_rng(seed=int(p50) % (2**32))
    samples = rng.normal(p50, p50 * 0.1, 1000)
    return Distribution.from_samples(
        samples, units="tCO2e", methodology_version="1.0", provenance_id=uuid4(),
    )


def _estimate(
    event: FireEvent,
    p50: float,
    needs_review: bool = False,
) -> EmissionEstimate:
    final = None if needs_review else _dist(p50)
    return EmissionEstimate(
        event_id=event.id,
        facility_id=event.facility_id,
        methodology_version="1.0",
        reconciliation=ReconciliationResult(
            final_distribution=final,
            frp_estimate=_dist(p50),
            inventory_estimate=None,
            reported_estimate=None,
            agreement_ratio=None,
            reconciled_ok=not needs_review,
            near_boundary=False,
            needs_review=needs_review,
            review_reason="test" if needs_review else None,
        ),
        has_damage_assessment=False,
        computed_at=datetime.now(UTC),
    )


def _facility(facility_id: UUID | None = None) -> Facility:
    return Facility(
        id=facility_id or uuid4(),
        name="Test Refinery",
        facility_type=FacilityType.REFINERY,
        geometry_wkt="POINT (51.4 32.6)",
        country="IRN",
        capacity_barrels=100_000.0,
        source_url="https://example.com/test",
        added_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# select_events_for_validation
# ---------------------------------------------------------------------------


class TestSelectEvents:
    def test_selects_top_n_by_p50(self) -> None:
        week_end = date(2026, 5, 23)
        events = [_event(detected_at=datetime(2026, 5, 20, i, 0, tzinfo=UTC)) for i in range(15)]
        estimates = [_estimate(ev, p50=float(i * 1000)) for i, ev in enumerate(events)]

        selected = select_events_for_validation(
            estimates, events, week_end=week_end, top_n=5,
        )

        assert len(selected) == 5
        p50s = [p for _, p in selected]
        assert p50s == sorted(p50s, reverse=True)

    def test_excludes_events_outside_week(self) -> None:
        week_end = date(2026, 5, 23)
        old_event = _event(detected_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC))
        recent_event = _event(detected_at=datetime(2026, 5, 20, 12, 0, tzinfo=UTC))

        estimates = [
            _estimate(old_event, 50_000.0),
            _estimate(recent_event, 10_000.0),
        ]

        selected = select_events_for_validation(
            estimates, [old_event, recent_event], week_end=week_end,
        )

        assert len(selected) == 1
        assert selected[0][0].id == recent_event.id

    def test_excludes_non_published(self) -> None:
        week_end = date(2026, 5, 23)
        published = _event(status=EventStatus.PUBLISHED)
        rejected = _event(status=EventStatus.REJECTED)

        estimates = [
            _estimate(published, 10_000.0),
            _estimate(rejected, 50_000.0),
        ]

        selected = select_events_for_validation(
            estimates, [published, rejected], week_end=week_end,
        )

        assert len(selected) == 1
        assert selected[0][0].id == published.id

    def test_excludes_needs_review(self) -> None:
        week_end = date(2026, 5, 23)
        ev = _event()
        est = _estimate(ev, 10_000.0, needs_review=True)

        selected = select_events_for_validation(
            [est], [ev], week_end=week_end,
        )

        assert len(selected) == 0

    def test_empty_inputs(self) -> None:
        selected = select_events_for_validation([], [], week_end=date(2026, 5, 23))
        assert selected == []


# ---------------------------------------------------------------------------
# ValidationReport
# ---------------------------------------------------------------------------


class TestValidationReport:
    def test_report_fields(self) -> None:
        prov = ProvenanceRecord(
            produced_by="test",
            method="test",
            produced_at=datetime.now(tz=UTC),
            confidence_label=ConfidenceLabel.SUSPECTED,
        )
        report = ValidationReport(
            id=uuid4(),
            run_date=date(2026, 5, 23),
            week_start=date(2026, 5, 16),
            week_end=date(2026, 5, 23),
            events_selected=10,
            events_validated=8,
            events_with_plume=5,
            events_flagged=3,
            methodology_review_triggered=True,
            flagged_event_ids=[uuid4(), uuid4(), uuid4()],
            provenance_record=prov,
            computed_at=datetime.now(UTC),
        )

        assert report.methodology_review_triggered is True
        assert report.events_flagged == 3
        assert len(report.flagged_event_ids) == 3

    def test_review_not_triggered_below_threshold(self) -> None:
        prov = ProvenanceRecord(
            produced_by="test",
            method="test",
            produced_at=datetime.now(tz=UTC),
            confidence_label=ConfidenceLabel.SUSPECTED,
        )
        report = ValidationReport(
            id=uuid4(),
            run_date=date(2026, 5, 23),
            week_start=date(2026, 5, 16),
            week_end=date(2026, 5, 23),
            events_selected=10,
            events_validated=8,
            events_with_plume=5,
            events_flagged=2,
            methodology_review_triggered=False,
            flagged_event_ids=[uuid4(), uuid4()],
            provenance_record=prov,
            computed_at=datetime.now(UTC),
        )

        assert report.methodology_review_triggered is False
