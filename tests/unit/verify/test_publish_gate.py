"""Tests for the confidence-gated publish gate.

Covers every routing decision defined in CLAUDE.md §"Confidence-Gated
Auto-Publish Policy":

  - Confirmed/Verified → auto-publish (PUBLISHED)
  - Reported/Suspected/Claimed → hold queue (PENDING_REVIEW)
  - Missing ProvenanceRecord chain → reject with reason
  - Distribution with <10,000 samples → reject with reason
  - Every transition appends to publication_log
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pytest

from wced.models.event import DetectionSource, EventStatus, FireEvent
from wced.models.provenance import (
    ConfidenceLabel,
    ProvenanceRecord,
    Source,
    SourceType,
)
from wced.provenance.store import InMemoryProvenanceStore
from wced.quantify.distribution import Distribution
from wced.quantify.reconcile import ReconciliationResult, reconcile_estimates
from wced.verify.editorial import InMemoryReviewQueue, PublishDecision, publish_gate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(tz=UTC)


def _event(
    confidence: ConfidenceLabel = ConfidenceLabel.CONFIRMED,
    provenance_id=None,
) -> FireEvent:
    now = _now()
    return FireEvent(
        facility_id=uuid4(),
        detected_at=now,
        last_seen_at=now,
        peak_frp_mw=75.0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=confidence,
        status=EventStatus.PENDING_REVIEW,
        provenance_id=provenance_id or uuid4(),
        created_at=now,
        updated_at=now,
    )


def _build_store_with_chain(provenance_id=None):
    """Build a provenance store with a complete chain (Source → Record)."""
    store = InMemoryProvenanceStore()
    source = Source(
        source_type=SourceType.SATELLITE,
        identifier="S2A_MSIL2A_20260301T000000",
        retrieved_at=_now(),
        retrieved_by="wced.ingest.sentinel2",
        content_hash="abc123",
    )
    store.record_source(source)
    record = ProvenanceRecord(
        id=provenance_id or uuid4(),
        produced_by="wced.quantify.frp",
        inputs=[source.id],
        method="frp_to_co2_v1.0",
        parameters={"n_samples": 10_000},
        produced_at=_now(),
        confidence_label=ConfidenceLabel.CONFIRMED,
    )
    store.record_provenance(record)
    return store, record


def _distribution(n_samples: int = 10_000, provenance_id=None) -> Distribution:
    rng = np.random.default_rng(42)
    return Distribution.from_samples(
        rng.normal(100.0, 10.0, n_samples),
        units="tCO2e",
        methodology_version="1.1.0",
        provenance_id=provenance_id or uuid4(),
    )


def _reconciliation(
    *,
    frp_p50: float | None,
    inv_p50: float | None,
    methodology_version: str = "1.1.0",
) -> ReconciliationResult:
    """Build a ReconciliationResult with controllable ρ = inv_p50 / frp_p50.

    Constant distributions give exact percentiles so the agreement ratio is
    deterministic. Passing one of ``frp_p50``/``inv_p50`` as None exercises the
    single-method path (no ratio to test).
    """
    frp = (
        Distribution.constant(frp_p50, "tCO2e", methodology_version, uuid4())
        if frp_p50 is not None
        else None
    )
    inv = (
        Distribution.constant(inv_p50, "tCO2e", methodology_version, uuid4())
        if inv_p50 is not None
        else None
    )
    return reconcile_estimates(_event(), frp, inv, None)


# ---------------------------------------------------------------------------
# Routing by confidence label
# ---------------------------------------------------------------------------


class TestConfidenceRouting:
    """Confirmed/Verified auto-publish; Reported/Suspected/Claimed hold."""

    def test_confirmed_auto_publishes(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "publish"
        assert decision.reason is None

    def test_verified_auto_publishes(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.VERIFIED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "publish"

    def test_reported_routes_to_hold_queue(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.REPORTED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "hold"
        assert "REPORTED" in decision.reason

    def test_suspected_routes_to_hold_queue(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.SUSPECTED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "hold"
        assert "SUSPECTED" in decision.reason

    def test_claimed_routes_to_hold_queue(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CLAIMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "hold"
        assert "CLAIMED" in decision.reason


# ---------------------------------------------------------------------------
# Provenance gate
# ---------------------------------------------------------------------------


class TestProvenanceGate:
    """Reject any estimate lacking a complete ProvenanceRecord chain."""

    def test_missing_provenance_record_rejects(self) -> None:
        store = InMemoryProvenanceStore()
        orphan_id = uuid4()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=orphan_id)
        dist = _distribution(provenance_id=orphan_id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "reject"
        assert "provenance" in decision.reason.lower()

    def test_provenance_chain_with_no_source_rejects(self) -> None:
        """A ProvenanceRecord with empty inputs (no upstream Source) is incomplete."""
        store = InMemoryProvenanceStore()
        record = ProvenanceRecord(
            produced_by="wced.quantify.frp",
            inputs=[],
            method="frp_to_co2_v1.0",
            parameters={},
            produced_at=_now(),
            confidence_label=ConfidenceLabel.CONFIRMED,
        )
        store.record_provenance(record)
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "reject"
        assert "source" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Distribution gate
# ---------------------------------------------------------------------------


class TestDistributionGate:
    """Reject any Distribution with <10,000 Monte Carlo samples."""

    def test_too_few_samples_rejects(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(n_samples=9_999, provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "reject"
        assert "10,000" in decision.reason or "10000" in decision.reason

    def test_exactly_10000_samples_passes(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(n_samples=10_000, provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "publish"

    def test_more_than_10000_samples_passes(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(n_samples=50_000, provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "publish"

    def test_none_samples_rejects(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(n_samples=10_000, provenance_id=record.id).without_samples()

        decision = publish_gate(event, dist, store)

        assert decision.action == "reject"
        assert "sample" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Gate priority: hard rejections before routing
# ---------------------------------------------------------------------------


class TestGatePriority:
    """Provenance/Distribution rejections take precedence over confidence routing."""

    def test_confirmed_but_missing_provenance_still_rejects(self) -> None:
        store = InMemoryProvenanceStore()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=uuid4())
        dist = _distribution()

        decision = publish_gate(event, dist, store)

        assert decision.action == "reject"

    def test_confirmed_but_too_few_samples_still_rejects(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(n_samples=100, provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "reject"


# ---------------------------------------------------------------------------
# PublishDecision model
# ---------------------------------------------------------------------------


class TestPublishDecision:
    def test_publish_decision_fields(self) -> None:
        d = PublishDecision(action="publish", reason=None)
        assert d.action == "publish"
        assert d.reason is None

    def test_hold_decision_requires_reason(self) -> None:
        d = PublishDecision(action="hold", reason="Confidence is REPORTED")
        assert d.reason is not None

    def test_reject_decision_requires_reason(self) -> None:
        d = PublishDecision(action="reject", reason="Missing provenance chain")
        assert d.reason is not None


# ---------------------------------------------------------------------------
# Integration: publish_gate + InMemoryReviewQueue
# ---------------------------------------------------------------------------


class TestPublishGateWithQueue:
    """End-to-end: publish_gate decides, queue executes, publication_log records."""

    def test_auto_publish_goes_through_queue(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        queue = InMemoryReviewQueue()

        decision = publish_gate(event, dist, store)
        assert decision.action == "publish"

        queue.submit(event, reviewer="pipeline")
        result = queue.approve(event.id, reviewer="publish_gate:auto")

        assert result.status is EventStatus.PUBLISHED
        pub_log = queue.publication_log
        approve_entries = [e for e in pub_log if e["action"] == "approve"]
        assert len(approve_entries) == 1
        assert approve_entries[0]["actor"] == "publish_gate:auto"

    def test_hold_stays_pending_review(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.REPORTED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        queue = InMemoryReviewQueue()

        decision = publish_gate(event, dist, store)
        assert decision.action == "hold"

        queue.submit(event, reviewer="pipeline")
        result = queue.get(event.id)

        assert result.status is EventStatus.PENDING_REVIEW

    def test_reject_goes_to_rejected_with_reason(self) -> None:
        store = InMemoryProvenanceStore()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=uuid4())
        dist = _distribution()
        queue = InMemoryReviewQueue()

        decision = publish_gate(event, dist, store)
        assert decision.action == "reject"

        queue.submit(event, reviewer="pipeline")
        result = queue.reject(
            event.id, reviewer="publish_gate:auto", reason=decision.reason,
        )

        assert result.status is EventStatus.REJECTED
        pub_log = queue.publication_log
        reject_entries = [e for e in pub_log if e["action"] == "reject"]
        assert len(reject_entries) == 1
        assert reject_entries[0]["reason"] == decision.reason


# ---------------------------------------------------------------------------
# Cross-method reconciliation gate (Gap 1.3, CLAUDE.md gate #4, methodology §3.5)
# ---------------------------------------------------------------------------


class TestCrossMethodGate:
    """Bottom-up vs top-down divergence beyond tolerance routes to review."""

    def test_within_tolerance_publishes(self) -> None:
        """ρ=1.5 is inside the default band [0.5, 2.0] → auto-publish."""
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        recon = _reconciliation(frp_p50=100.0, inv_p50=150.0)
        assert recon.agreement_ratio == pytest.approx(1.5)

        decision = publish_gate(event, dist, store, reconciliation=recon)

        assert decision.action == "publish"
        assert decision.reason is None

    def test_beyond_tolerance_routes_to_review(self) -> None:
        """ρ=3.0 is outside [0.5, 2.0]; reconcile flags it → hold, not publish."""
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        recon = _reconciliation(frp_p50=100.0, inv_p50=300.0)
        assert recon.needs_review is True

        decision = publish_gate(event, dist, store, reconciliation=recon)

        assert decision.action == "hold"
        assert "divergence" in decision.reason.lower()

    def test_low_ratio_routes_to_review(self) -> None:
        """ρ=0.4 (< 0.5) also diverges → hold."""
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        recon = _reconciliation(frp_p50=100.0, inv_p50=40.0)

        decision = publish_gate(event, dist, store, reconciliation=recon)

        assert decision.action == "hold"

    def test_configurable_tolerance_stricter_than_reconcile(self) -> None:
        """A gate tolerance of 1.5 holds ρ=1.9 even though reconcile passed it."""
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        recon = _reconciliation(frp_p50=100.0, inv_p50=190.0)
        assert recon.needs_review is False  # 1.9 is within reconcile's [0.5, 2.0]

        decision = publish_gate(
            event, dist, store, reconciliation=recon, cross_method_tolerance=1.5,
        )

        assert decision.action == "hold"
        assert "1.9" in decision.reason or "1.90" in decision.reason

    def test_configurable_tolerance_looser_publishes(self) -> None:
        """A looser gate tolerance still honours reconcile's hard flag.

        Even with tolerance=5.0, an estimate reconcile already flagged
        (final_distribution=None) must not auto-publish.
        """
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        recon = _reconciliation(frp_p50=100.0, inv_p50=300.0)  # ρ=3.0, flagged

        decision = publish_gate(
            event, dist, store, reconciliation=recon, cross_method_tolerance=5.0,
        )

        assert decision.action == "hold"

    def test_single_method_estimate_publishes(self) -> None:
        """Only one method available → no ratio → no cross-method veto."""
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        recon = _reconciliation(frp_p50=100.0, inv_p50=None)
        assert recon.agreement_ratio is None

        decision = publish_gate(event, dist, store, reconciliation=recon)

        assert decision.action == "publish"

    def test_no_reconciliation_is_backward_compatible(self) -> None:
        """Omitting the reconciliation argument leaves prior behaviour intact."""
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)

        decision = publish_gate(event, dist, store)

        assert decision.action == "publish"

    def test_hard_rejections_precede_cross_method(self) -> None:
        """A missing provenance chain rejects even when methods diverge."""
        store = InMemoryProvenanceStore()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=uuid4())
        dist = _distribution()
        recon = _reconciliation(frp_p50=100.0, inv_p50=300.0)

        decision = publish_gate(event, dist, store, reconciliation=recon)

        assert decision.action == "reject"

    def test_low_confidence_holds_regardless_of_reconciliation(self) -> None:
        """A REPORTED event holds on confidence before cross-method is consulted."""
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.REPORTED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        recon = _reconciliation(frp_p50=100.0, inv_p50=150.0)  # within tolerance

        decision = publish_gate(event, dist, store, reconciliation=recon)

        assert decision.action == "hold"
        assert "REPORTED" in decision.reason

    def test_invalid_tolerance_raises(self) -> None:
        store, record = _build_store_with_chain()
        event = _event(ConfidenceLabel.CONFIRMED, provenance_id=record.id)
        dist = _distribution(provenance_id=record.id)
        recon = _reconciliation(frp_p50=100.0, inv_p50=150.0)

        with pytest.raises(ValueError, match="cross_method_tolerance"):
            publish_gate(
                event, dist, store, reconciliation=recon, cross_method_tolerance=0.0,
            )
