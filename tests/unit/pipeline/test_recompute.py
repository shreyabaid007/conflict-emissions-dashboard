"""Tests for wced.pipeline.recompute — confidence recomputation, routing,
and report generation.

All tests are offline (no database, no network). The recompute module
provides pure functions that can be tested with constructed inputs.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from wced.models.event import EventStatus, FireEvent
from wced.models.provenance import ConfidenceLabel
from wced.pipeline.recompute import (
    EventRecomputeResult,
    PendingReviewTransition,
    RecomputeReport,
    generate_recompute_report_md,
    recompute_confidence_label,
    route_events_to_pending_review,
)


_T0 = datetime(2026, 3, 15, 6, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# recompute_confidence_label — decision table under v1.1.0
# ---------------------------------------------------------------------------


class TestRecomputeConfidenceLabel:
    """Each row of the v1.1.0 decision table, with ENABLE_ACLED on and off."""

    def test_persistent_s2_gdelt_yields_confirmed(self) -> None:
        result = recompute_confidence_label(
            n_overpasses=2,
            s2_confirms_fire=True,
            has_acled_corroboration=False,
            has_gdelt_corroboration=True,
            enable_acled=False,
        )
        assert result is ConfidenceLabel.CONFIRMED

    def test_persistent_s2_acled_enabled_yields_confirmed(self) -> None:
        result = recompute_confidence_label(
            n_overpasses=2,
            s2_confirms_fire=True,
            has_acled_corroboration=True,
            has_gdelt_corroboration=False,
            enable_acled=True,
        )
        assert result is ConfidenceLabel.CONFIRMED

    def test_persistent_s2_acled_disabled_ignores_acled(self) -> None:
        """ACLED corroboration is ignored when enable_acled=False."""
        result = recompute_confidence_label(
            n_overpasses=2,
            s2_confirms_fire=True,
            has_acled_corroboration=True,
            has_gdelt_corroboration=False,
            enable_acled=False,
        )
        assert result is ConfidenceLabel.VERIFIED

    def test_persistent_s2_no_corroboration_yields_verified(self) -> None:
        result = recompute_confidence_label(
            n_overpasses=2,
            s2_confirms_fire=True,
            has_acled_corroboration=False,
            has_gdelt_corroboration=False,
            enable_acled=False,
        )
        assert result is ConfidenceLabel.VERIFIED

    def test_persistent_no_s2_gdelt_yields_verified(self) -> None:
        result = recompute_confidence_label(
            n_overpasses=2,
            s2_confirms_fire=False,
            has_acled_corroboration=False,
            has_gdelt_corroboration=True,
            enable_acled=False,
        )
        assert result is ConfidenceLabel.VERIFIED

    def test_persistent_no_s2_no_corroboration_yields_reported(self) -> None:
        result = recompute_confidence_label(
            n_overpasses=2,
            s2_confirms_fire=False,
            has_acled_corroboration=False,
            has_gdelt_corroboration=False,
            enable_acled=False,
        )
        assert result is ConfidenceLabel.REPORTED

    def test_single_overpass_yields_suspected(self) -> None:
        result = recompute_confidence_label(
            n_overpasses=1,
            s2_confirms_fire=True,
            has_acled_corroboration=True,
            has_gdelt_corroboration=True,
            enable_acled=True,
        )
        assert result is ConfidenceLabel.SUSPECTED

    def test_idempotent_same_inputs_same_output(self) -> None:
        """Same inputs always produce the same label (deterministic)."""
        kwargs = dict(
            n_overpasses=2,
            s2_confirms_fire=True,
            has_acled_corroboration=False,
            has_gdelt_corroboration=True,
            enable_acled=False,
        )
        r1 = recompute_confidence_label(**kwargs)
        r2 = recompute_confidence_label(**kwargs)
        assert r1 is r2


# ---------------------------------------------------------------------------
# route_events_to_pending_review
# ---------------------------------------------------------------------------


def _make_fire_event(
    status: EventStatus = EventStatus.PUBLISHED,
) -> FireEvent:
    now = _T0
    return FireEvent(
        facility_id=uuid4(),
        detected_at=now,
        last_seen_at=now,
        peak_frp_mw=50.0,
        detection_source="FIRMS_VIIRS",
        confidence_label=ConfidenceLabel.VERIFIED,
        status=status,
        provenance_id=uuid4(),
        created_at=now,
        updated_at=now,
    )


class TestRouteEventsToPendingReview:
    def test_published_event_is_routed(self) -> None:
        event = _make_fire_event(EventStatus.PUBLISHED)
        transitions = route_events_to_pending_review(
            [event], methodology_version="1.1.0"
        )
        assert len(transitions) == 1
        assert transitions[0].previous_status is EventStatus.PUBLISHED
        assert transitions[0].new_status is EventStatus.PENDING_REVIEW

    def test_retracted_event_is_skipped(self) -> None:
        event = _make_fire_event(EventStatus.RETRACTED)
        transitions = route_events_to_pending_review(
            [event], methodology_version="1.1.0"
        )
        assert len(transitions) == 0

    def test_pending_event_is_still_routed(self) -> None:
        event = _make_fire_event(EventStatus.PENDING_REVIEW)
        transitions = route_events_to_pending_review(
            [event], methodology_version="1.1.0"
        )
        assert len(transitions) == 1

    def test_nothing_auto_published(self) -> None:
        """No transition should have a to_state of PUBLISHED."""
        events = [
            _make_fire_event(EventStatus.PUBLISHED),
            _make_fire_event(EventStatus.PUBLISHED),
        ]
        transitions = route_events_to_pending_review(
            events, methodology_version="1.1.0"
        )
        for t in transitions:
            assert t.new_status is EventStatus.PENDING_REVIEW
            assert t.new_status is not EventStatus.PUBLISHED

    def test_publication_log_entry_format(self) -> None:
        event = _make_fire_event(EventStatus.PUBLISHED)
        transitions = route_events_to_pending_review(
            [event], methodology_version="1.1.0"
        )
        entry = transitions[0].as_publication_log_entry()
        assert entry["target_type"] == "fire_event"
        assert entry["target_id"] == event.id
        assert entry["from_state"] == "PUBLISHED"
        assert entry["to_state"] == "PENDING_REVIEW"
        assert entry["action"] == "recompute_route_to_review"
        assert entry["methodology_version"] == "1.1.0"


# ---------------------------------------------------------------------------
# RecomputeReport
# ---------------------------------------------------------------------------


def _make_result(
    old_label: ConfidenceLabel = ConfidenceLabel.VERIFIED,
    new_label: ConfidenceLabel = ConfidenceLabel.CONFIRMED,
    old_p50: float = 100.0,
    new_p50: float = 95.0,
    had_acled: bool = False,
    had_gdelt: bool = True,
    had_s2: bool = True,
) -> EventRecomputeResult:
    return EventRecomputeResult(
        event_id=uuid4(),
        facility_name="Test Facility",
        old_label=old_label,
        new_label=new_label,
        old_p50_tco2e=old_p50,
        new_p50_tco2e=new_p50,
        had_acled_corroboration=had_acled,
        had_gdelt_corroboration=had_gdelt,
        had_s2_fire=had_s2,
        label_changed=old_label != new_label,
        routed_to_pending=True,
    )


class TestRecomputeReport:
    def test_counts(self) -> None:
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            events=[
                _make_result(
                    old_label=ConfidenceLabel.VERIFIED,
                    new_label=ConfidenceLabel.CONFIRMED,
                ),
                _make_result(
                    old_label=ConfidenceLabel.CONFIRMED,
                    new_label=ConfidenceLabel.CONFIRMED,
                ),
            ],
        )
        assert report.total_events == 2
        assert report.labels_changed == 1
        assert report.labels_unchanged == 1

    def test_upgraded_count(self) -> None:
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            events=[
                _make_result(
                    old_label=ConfidenceLabel.VERIFIED,
                    new_label=ConfidenceLabel.CONFIRMED,
                ),
            ],
        )
        assert len(report.upgraded) == 1
        assert len(report.downgraded) == 0

    def test_downgraded_count(self) -> None:
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            events=[
                _make_result(
                    old_label=ConfidenceLabel.CONFIRMED,
                    new_label=ConfidenceLabel.VERIFIED,
                ),
            ],
        )
        assert len(report.downgraded) == 1
        assert len(report.upgraded) == 0

    def test_acled_only_events(self) -> None:
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            events=[
                _make_result(had_acled=True, had_gdelt=False),
                _make_result(had_acled=True, had_gdelt=True),
                _make_result(had_acled=False, had_gdelt=True),
            ],
        )
        assert len(report.acled_only_events) == 1

    def test_totals(self) -> None:
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            events=[
                _make_result(old_p50=100.0, new_p50=90.0),
                _make_result(old_p50=200.0, new_p50=180.0),
            ],
        )
        assert report.old_total_p50 == pytest.approx(300.0)
        assert report.new_total_p50 == pytest.approx(270.0)


# ---------------------------------------------------------------------------
# generate_recompute_report_md
# ---------------------------------------------------------------------------


class TestGenerateReport:
    def test_report_contains_version(self) -> None:
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            finished_at=_T0,
            events=[],
        )
        md = generate_recompute_report_md(report)
        assert "v1.1.0" in md

    def test_report_contains_routing_note(self) -> None:
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            finished_at=_T0,
            events=[_make_result()],
        )
        md = generate_recompute_report_md(report)
        assert "PENDING_REVIEW" in md
        assert "No events were auto-published" in md

    def test_report_contains_acled_section(self) -> None:
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            finished_at=_T0,
            events=[_make_result(had_acled=True, had_gdelt=False)],
        )
        md = generate_recompute_report_md(report)
        assert "ACLED-only corroboration" in md
        assert "ENABLE_ACLED=False" in md

    def test_report_per_event_table(self) -> None:
        result = _make_result(old_p50=42.5, new_p50=38.0)
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            finished_at=_T0,
            events=[result],
        )
        md = generate_recompute_report_md(report)
        assert "42.5" in md
        assert "38.0" in md
        assert str(result.event_id) in md

    def test_empty_report(self) -> None:
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=uuid4(),
            started_at=_T0,
            finished_at=_T0,
            events=[],
        )
        md = generate_recompute_report_md(report)
        assert "Events processed: 0" in md
        assert "No events had ACLED-only corroboration" in md


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Recompute with fixed inputs + seed produces identical results."""

    def test_confidence_label_is_deterministic(self) -> None:
        kwargs = dict(
            n_overpasses=2,
            s2_confirms_fire=True,
            has_acled_corroboration=True,
            has_gdelt_corroboration=False,
            enable_acled=False,
        )
        results = [recompute_confidence_label(**kwargs) for _ in range(10)]
        assert all(r is results[0] for r in results)

    def test_report_generation_is_deterministic(self) -> None:
        run_id = uuid4()
        events = [
            _make_result(old_p50=100.0, new_p50=90.0),
            _make_result(old_p50=200.0, new_p50=180.0),
        ]
        report = RecomputeReport(
            methodology_version="1.1.0",
            run_id=run_id,
            started_at=_T0,
            finished_at=_T0,
            events=events,
        )
        md1 = generate_recompute_report_md(report)
        md2 = generate_recompute_report_md(report)
        assert md1 == md2

    def test_routing_is_deterministic(self) -> None:
        events = [_make_fire_event(EventStatus.PUBLISHED)]
        t1 = route_events_to_pending_review(events, methodology_version="1.1.0")
        t2 = route_events_to_pending_review(events, methodology_version="1.1.0")
        assert len(t1) == len(t2)
        assert t1[0].event_id == t2[0].event_id
        assert t1[0].previous_status == t2[0].previous_status
