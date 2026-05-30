"""Tests for wced.quantify.reconcile.reconcile_estimates.

Hand-computed expected values follow methodology v1.0 §3.5.
The Shahran worked example: FRP p50≈69 000 tCO2e, inventory p50≈51 000 tCO2e,
ρ = 51 000 / 69 000 ≈ 0.739 → agreement, envelope p50 between the two inputs.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import numpy as np
import pytest

from wced.models.event import DetectionSource, FireEvent
from wced.models.provenance import ConfidenceLabel
from wced.quantify.distribution import Distribution
from wced.quantify.reconcile import reconcile_estimates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event() -> FireEvent:
    t0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    return FireEvent(
        facility_id=uuid4(),
        detected_at=t0,
        last_seen_at=t0 + timedelta(hours=6),
        peak_frp_mw=120.0,
        total_frp_integral_mj=5000.0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.REPORTED,
        provenance_id=uuid4(),
        created_at=t0,
        updated_at=t0,
    )


def _dist(p50: float, spread_pct: float = 0.20, n: int = 10_000) -> Distribution:
    """Build a normal Distribution centred on *p50* with ±spread CV.

    The spread is intentionally small so p50 of the resulting distribution
    tracks the requested value tightly, making ratio assertions deterministic.
    """
    rng = np.random.default_rng(seed=int(p50) % (2**32))
    std = p50 * spread_pct
    samples = rng.normal(p50, std, n)
    return Distribution.from_samples(
        samples,
        units="tCO2e",
        methodology_version="1.0",
        provenance_id=uuid4(),
    )


# ---------------------------------------------------------------------------
# Shahran worked example (§3.5)
# ---------------------------------------------------------------------------


def test_shahran_example_reconciled_ok() -> None:
    """ρ ≈ 0.739 → agreement; envelope p50 between the two inputs."""
    frp = _dist(69_000.0)
    inv = _dist(51_000.0)

    result = reconcile_estimates(_event(), frp, inv, None)

    rho = result.agreement_ratio
    assert rho is not None
    # ρ convention is inventory/FRP per methodology §3.5.
    assert abs(rho - 51_000 / 69_000) < 0.05, f"unexpected ρ={rho:.4f}"
    assert result.reconciled_ok is True
    assert result.near_boundary is False
    assert result.needs_review is False
    assert result.final_distribution is not None
    # Envelope p50 must lie between the two input medians.
    assert 51_000 < result.final_distribution.p50 < 69_000


def test_shahran_envelope_sample_count() -> None:
    """Envelope pools both sample arrays so len(samples) == 2 × n."""
    n = 500
    frp = _dist(69_000.0, n=n)
    inv = _dist(51_000.0, n=n)

    result = reconcile_estimates(_event(), frp, inv, None)

    assert result.final_distribution is not None
    assert result.final_distribution.samples is not None
    assert len(result.final_distribution.samples) == 2 * n


# ---------------------------------------------------------------------------
# Single-method paths
# ---------------------------------------------------------------------------


def test_frp_only() -> None:
    """When only the FRP estimate is present, final equals FRP (§3.5 case 1)."""
    frp = _dist(10_000.0)
    result = reconcile_estimates(_event(), frp, None, None)

    assert result.reconciled_ok is True
    assert result.needs_review is False
    assert result.agreement_ratio is None
    assert result.near_boundary is False
    assert result.final_distribution is frp


def test_inventory_only() -> None:
    """When only the inventory estimate is present, final equals inventory (§3.5 case 2)."""
    inv = _dist(10_000.0)
    result = reconcile_estimates(_event(), None, inv, None)

    assert result.reconciled_ok is True
    assert result.needs_review is False
    assert result.agreement_ratio is None
    assert result.final_distribution is inv


def test_property_single_estimate_is_final() -> None:
    """Property: providing exactly one estimate always returns it as final."""
    for frp_arg, inv_arg in [(_dist(5_000.0), None), (None, _dist(5_000.0))]:
        result = reconcile_estimates(_event(), frp_arg, inv_arg, None)
        expected = frp_arg if frp_arg is not None else inv_arg
        assert result.final_distribution is expected
        assert result.reconciled_ok is True


# ---------------------------------------------------------------------------
# Disagreement path
# ---------------------------------------------------------------------------


def test_disagreement_rho_3() -> None:
    """ρ = 3.0 > 2.0 → needs_review=True, final_distribution=None (§3.5)."""
    frp = _dist(10_000.0)
    inv = _dist(30_000.0)  # p50/p50 ≈ 3.0

    result = reconcile_estimates(_event(), frp, inv, None)

    assert result.needs_review is True
    assert result.reconciled_ok is False
    assert result.final_distribution is None
    assert result.agreement_ratio is not None
    assert result.agreement_ratio > 2.0
    assert result.review_reason is not None
    assert "ρ" in result.review_reason


def test_disagreement_rho_below_half() -> None:
    """ρ < 0.5 also triggers needs_review (inventory much lower than FRP)."""
    frp = _dist(30_000.0)
    inv = _dist(10_000.0)  # p50/p50 ≈ 0.33

    result = reconcile_estimates(_event(), frp, inv, None)

    assert result.needs_review is True
    assert result.final_distribution is None
    assert result.agreement_ratio is not None
    assert result.agreement_ratio < 0.5


# ---------------------------------------------------------------------------
# Near-boundary flag
# ---------------------------------------------------------------------------


def test_near_boundary_lower_zone_rho_052() -> None:
    """ρ = 0.52 ∈ [0.50, 0.55] → reconciled_ok=True, near_boundary=True (§3.5)."""
    frp = _dist(10_000.0)
    inv = _dist(5_200.0)  # p50/p50 ≈ 0.52

    result = reconcile_estimates(_event(), frp, inv, None)

    assert result.reconciled_ok is True
    assert result.near_boundary is True
    assert result.needs_review is False
    assert result.final_distribution is not None


def test_near_boundary_upper_zone() -> None:
    """ρ = 1.90 ∈ [1.82, 2.00] → reconciled_ok=True, near_boundary=True."""
    frp = _dist(10_000.0)
    inv = _dist(19_000.0)  # p50/p50 ≈ 1.9

    result = reconcile_estimates(_event(), frp, inv, None)

    assert result.reconciled_ok is True
    assert result.near_boundary is True
    assert result.needs_review is False


def test_interior_agreement_not_near_boundary() -> None:
    """ρ = 1.0 is well inside the band — near_boundary must be False."""
    frp = _dist(10_000.0)
    inv = _dist(10_000.0)

    result = reconcile_estimates(_event(), frp, inv, None)

    assert result.near_boundary is False
    assert result.reconciled_ok is True


# ---------------------------------------------------------------------------
# Reported estimate handling
# ---------------------------------------------------------------------------


def test_reported_estimate_preserved_not_in_final() -> None:
    """Reported estimate is stored on the result but does not affect final_distribution."""
    frp = _dist(10_000.0)
    reported = _dist(8_000.0)

    # FRP-only scenario: final should be frp regardless of reported.
    result = reconcile_estimates(_event(), frp, None, reported)

    assert result.reported_estimate is reported
    assert result.final_distribution is frp


def test_reported_estimate_with_both_methods() -> None:
    """Reported estimate is preserved alongside an envelope final_distribution."""
    frp = _dist(10_000.0)
    inv = _dist(12_000.0)
    reported = _dist(11_000.0)

    result = reconcile_estimates(_event(), frp, inv, reported)

    assert result.reported_estimate is reported
    # Final is the envelope, not the reported value.
    assert result.final_distribution is not reported
    assert result.final_distribution is not None


# ---------------------------------------------------------------------------
# Result metadata
# ---------------------------------------------------------------------------


def test_methodology_section_is_35() -> None:
    result = reconcile_estimates(_event(), _dist(10_000.0), None, None)
    assert result.methodology_section == "3.5"


def test_all_inputs_preserved() -> None:
    """All three input estimates appear on the result regardless of outcome."""
    frp = _dist(10_000.0)
    inv = _dist(30_000.0)  # disagreement
    rep = _dist(15_000.0)

    result = reconcile_estimates(_event(), frp, inv, rep)

    assert result.frp_estimate is frp
    assert result.inventory_estimate is inv
    assert result.reported_estimate is rep


# ---------------------------------------------------------------------------
# Envelope provenance determinism
# ---------------------------------------------------------------------------


def test_envelope_provenance_deterministic() -> None:
    """Same (frp_prov, inv_prov) inputs → same envelope provenance_id."""
    frp = _dist(10_000.0)
    inv = _dist(12_000.0)

    r1 = reconcile_estimates(_event(), frp, inv, None)
    r2 = reconcile_estimates(_event(), frp, inv, None)

    assert r1.final_distribution is not None
    assert r2.final_distribution is not None
    assert r1.final_distribution.provenance_id == r2.final_distribution.provenance_id


def test_envelope_units_and_version_inherited() -> None:
    """Envelope carries the same units and methodology_version as its inputs."""
    frp = _dist(10_000.0)
    inv = _dist(12_000.0)

    result = reconcile_estimates(_event(), frp, inv, None)

    assert result.final_distribution is not None
    assert result.final_distribution.units == "tCO2e"
    assert result.final_distribution.methodology_version == "1.0"
