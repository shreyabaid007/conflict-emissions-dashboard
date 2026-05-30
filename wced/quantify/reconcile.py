"""Reconciliation of FRP and inventory emission estimates.

Implements methodology/v1.0.pdf §3.5. When both FRP-based and
inventory-based estimates are available for the same event, this module
computes their ratio ρ = p50(inventory) / p50(FRP) and either:
  - Produces an ENVELOPE distribution (pooled sample arrays) when the two
    estimates agree (0.5 ≤ ρ ≤ 2.0), or
  - Flags the event for editorial review on disagreement (ρ < 0.5 or ρ > 2.0).

Reported (third-party) estimates are recorded as CLAIMED cross-checks and
never enter the headline arithmetic. See methodology §3.5 for the full
decision table and the rationale for the asymmetric near-boundary windows.
"""
from __future__ import annotations

import uuid
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from wced.models.event import FireEvent
from wced.quantify.distribution import Distribution

__all__ = ["ReconciliationResult", "reconcile_estimates"]

# Provenance namespace for reconciled envelope distributions.
# Stable across runs so the same (frp_prov, inv_prov) pair always yields
# the same derived provenance_id — see methodology §3.5.
_RECONCILE_PROV_NS = uuid.UUID("c2e3f4a5-0000-5000-8000-000000000005")

# Agreement band (§3.5 Table 1). ρ = p50(inventory) / p50(FRP).
_AGREE_LOW = 0.5
_AGREE_HIGH = 2.0

# Near-boundary sub-intervals within the agreement band (§3.5).
# Values in these ranges are flagged for extra editorial scrutiny even
# though they formally satisfy the agreement criterion.
_NEAR_LOW_HI = 0.55   # [_AGREE_LOW, _NEAR_LOW_HI] is the lower near-boundary zone
_NEAR_HI_LO = 1.82    # [_NEAR_HI_LO, _AGREE_HIGH] is the upper near-boundary zone


class ReconciliationResult(BaseModel):
    """Outcome of reconciling FRP and inventory estimates for one event.

    All input distributions are preserved regardless of outcome so that
    the editorial board can inspect both when ``needs_review=True``.

    Parameters
    ----------
    final_distribution : Distribution or None
        The headline estimate to be dashboarded.  None when
        ``needs_review=True`` — neither estimate is promoted until an
        editorial reviewer resolves the discrepancy.
    frp_estimate : Distribution or None
        FRP-based estimate as received from ``wced.quantify.frp``.
    inventory_estimate : Distribution or None
        Inventory-based estimate from ``wced.quantify.inventory``.
    reported_estimate : Distribution or None
        Third-party (CEOBS/CCI/news) estimate stored as CLAIMED
        cross-check only — not in headline arithmetic.
    agreement_ratio : float or None
        ρ = p50(inventory) / p50(FRP). None when only one method is
        available (ratio is undefined).
    reconciled_ok : bool
        True iff both estimates agree (0.5 ≤ ρ ≤ 2.0) or only one
        estimate is available.
    near_boundary : bool
        True iff ρ ∈ [0.50, 0.55] ∪ [1.82, 2.00], signalling that the
        ratio is within the agreement band but close to its edge.
        Always False when ``agreement_ratio`` is None.
    needs_review : bool
        True iff ρ < 0.5 or ρ > 2.0.  ``final_distribution`` is None
        in this case.
    review_reason : str or None
        Human-readable explanation of why editorial review is required.
        None when ``needs_review=False``.
    methodology_section : str
        Always ``"3.5"`` — records which methodology section governs
        this result.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    final_distribution: Distribution | None
    frp_estimate: Distribution | None
    inventory_estimate: Distribution | None
    reported_estimate: Distribution | None
    agreement_ratio: float | None
    reconciled_ok: bool
    near_boundary: bool
    needs_review: bool
    review_reason: str | None
    methodology_section: Literal["3.5"] = "3.5"


def _envelope(frp: Distribution, inv: Distribution) -> Distribution:
    """Pool samples from both distributions into a single envelope.

    Per methodology §3.5: the envelope is NOT a weighted average.
    It is the union of both MC sample arrays.  Percentiles are
    recomputed on the combined set so the headline interval spans the
    full range of uncertainty implied by both methods simultaneously.

    Raises
    ------
    ValueError
        If either distribution is missing its sample array, or if the
        two distributions have different methodology versions or units.
    """
    if frp.methodology_version != inv.methodology_version:
        raise ValueError(
            f"Cannot envelope Distributions from different methodology versions: "
            f"{frp.methodology_version!r} vs {inv.methodology_version!r}"
        )
    if frp.units != inv.units:
        raise ValueError(
            f"Cannot envelope Distributions with different units: "
            f"{frp.units!r} vs {inv.units!r}"
        )
    frp_samples = frp._require_samples()
    inv_samples = inv._require_samples()

    pooled = np.concatenate([frp_samples, inv_samples])

    provenance_id = uuid.uuid5(
        _RECONCILE_PROV_NS,
        f"envelope|{frp.provenance_id}⊕{inv.provenance_id}",
    )
    return Distribution.from_samples(
        pooled,
        units=frp.units,
        methodology_version=frp.methodology_version,
        provenance_id=provenance_id,
    )


def reconcile_estimates(
    event: FireEvent,
    frp_estimate: Distribution | None,
    inventory_estimate: Distribution | None,
    reported_estimate: Distribution | None,
) -> ReconciliationResult:
    """Reconcile FRP and inventory CO2 estimates for a single fire event.

    Implements methodology/v1.0.pdf §3.5 decision table:

    1. FRP only → final = FRP.
    2. Inventory only → final = inventory.
    3. Both available → compute ρ = p50(inventory) / p50(FRP):
       - 0.5 ≤ ρ ≤ 2.0 → envelope (pooled samples); reconciled_ok=True.
         Near-boundary windows [0.50, 0.55] ∪ [1.82, 2.00] trigger an
         additional near_boundary=True flag for editorial scrutiny.
       - ρ < 0.5 or ρ > 2.0 → needs_review=True; final_distribution=None.
    4. Reported estimate → stored as CLAIMED cross-check; never in
       headline arithmetic (§3.5, "Reported estimates").

    Parameters
    ----------
    event : FireEvent
        The fire event being quantified. Used only for contextual
        logging — the function is otherwise pure.
    frp_estimate : Distribution or None
        FRP-based CO2 estimate in tCO2e.  None if FIRMS data is absent.
    inventory_estimate : Distribution or None
        Inventory-based CO2 estimate in tCO2e.  None if facility
        capacity or fraction_destroyed is unavailable.
    reported_estimate : Distribution or None
        Third-party reported estimate.  Always recorded; never
        dashboarded without editorial conversion to Tier 1 evidence.

    Returns
    -------
    ReconciliationResult
        Full reconciliation record including all input estimates,
        the computed ratio, and the promoted final_distribution (or
        None with a review_reason if disagreement is detected).
    """
    both_present = frp_estimate is not None and inventory_estimate is not None

    if not both_present:
        # Single-method path — no ratio to compute.
        final = frp_estimate if frp_estimate is not None else inventory_estimate
        return ReconciliationResult(
            final_distribution=final,
            frp_estimate=frp_estimate,
            inventory_estimate=inventory_estimate,
            reported_estimate=reported_estimate,
            agreement_ratio=None,
            reconciled_ok=True,
            near_boundary=False,
            needs_review=False,
            review_reason=None,
        )

    # Both estimates present — compute ρ per §3.5 convention:
    # ratio = inventory / FRP  (NOT FRP / inventory).
    frp_p50 = frp_estimate.p50
    if frp_p50 == 0.0:
        raise ValueError(
            f"FRP estimate for event {event.id} has p50=0; "
            "cannot compute agreement ratio (division by zero). "
            "Check that the FRP integral is positive."
        )
    rho = inventory_estimate.p50 / frp_p50

    agrees = _AGREE_LOW <= rho <= _AGREE_HIGH
    near_boundary = agrees and (
        rho <= _NEAR_LOW_HI or rho >= _NEAR_HI_LO
    )

    if agrees:
        final = _envelope(frp_estimate, inventory_estimate)
        return ReconciliationResult(
            final_distribution=final,
            frp_estimate=frp_estimate,
            inventory_estimate=inventory_estimate,
            reported_estimate=reported_estimate,
            agreement_ratio=rho,
            reconciled_ok=True,
            near_boundary=near_boundary,
            needs_review=False,
            review_reason=None,
        )

    # Disagreement — neither estimate is promoted (§3.5).
    reason = (
        f"Agreement ratio ρ={rho:.3f} is outside the acceptable band "
        f"[{_AGREE_LOW}, {_AGREE_HIGH}] (methodology §3.5). "
        f"FRP p50={frp_p50:.1f} tCO2e, "
        f"inventory p50={inventory_estimate.p50:.1f} tCO2e. "
        "Both estimates are stored. A reviewer must reconcile the discrepancy "
        "in the event changelog before either estimate can be dashboarded."
    )
    return ReconciliationResult(
        final_distribution=None,
        frp_estimate=frp_estimate,
        inventory_estimate=inventory_estimate,
        reported_estimate=reported_estimate,
        agreement_ratio=rho,
        reconciled_ok=False,
        near_boundary=False,
        needs_review=True,
        review_reason=reason,
    )
