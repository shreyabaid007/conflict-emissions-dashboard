"""Anomaly-watch agent for published emission estimates.

CLAUDE.md §"Confidence-Gated Auto-Publish Policy" gate #5 requires a process
that monitors every published estimate and auto-retracts outliers to
``PENDING_REVIEW`` with a public "under review" note. This module implements
that gate.

Two independent checks decide whether an estimate is an outlier:

1. **Historical** — the new estimate's p50 is compared against the facility's
   prior *published* estimates using a robust, median/MAD-based modified
   z-score (Iglewicz & Hoaglin). Robust statistics are used deliberately so a
   single earlier anomaly does not poison the baseline. When the historical
   spread is degenerate (MAD = 0, e.g. all prior estimates identical) the check
   falls back to a multiplicative ratio band.

2. **Cross-method** — the new headline estimate is compared against the event's
   *other* method (FRP vs inventory). A large ratio between the two means the
   bottom-up and top-down numbers disagree even though one was published.

Thresholds are tuned conservatively (see ``AnomalyThresholds`` defaults): the
cost of a false retraction — pulling a correct number off the public dashboard
— is higher than the cost of letting a borderline estimate stand for one more
review cycle. Two design choices guard against false retractions:

- The historical check is skipped entirely until a facility has at least
  ``min_history`` prior estimates, so a facility's first few estimates are
  never auto-retracted for lack of a baseline.
- A **spread floor** prevents an artificially tight history (small MAD) from
  turning a modest deviation into a huge z-score. The effective robust scale is
  ``max(MAD/0.6745, spread_floor_frac * |median|)``, so a facility whose past
  estimates happened to cluster tightly does not get a hair-trigger detector.
  A gross magnitude change (ratio outside ``history_ratio_band``) is caught by a
  separate fallback even when the z-score stays under threshold.

This module is pure with respect to the numeric assessment
(``evaluate_published_estimate``); the only side effect is the optional queue
transition performed by ``AnomalyWatch.review``.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from wced.models.event import EventStatus, FireEvent

if TYPE_CHECKING:
    from wced.quantify.distribution import Distribution
    from wced.verify.editorial import ReviewQueueProtocol

log = logging.getLogger(__name__)

__all__ = [
    "AnomalyThresholds",
    "AnomalyAssessment",
    "AnomalyWatch",
    "evaluate_published_estimate",
]

# Scale factor that makes the MAD a consistent estimator of the standard
# deviation for normally distributed data (Iglewicz & Hoaglin 1993).
_MAD_TO_SIGMA = 0.6745


@dataclass(frozen=True)
class AnomalyThresholds:
    """Tunable thresholds for the anomaly checks.

    Defaults are deliberately conservative to minimise false retractions.

    Parameters
    ----------
    min_history : int
        Minimum number of prior published estimates required before the
        historical check runs at all. Below this, the historical check is
        skipped (a facility's first estimates have no baseline).
    history_z_threshold : float
        Modified z-score above which the new estimate is a historical outlier.
        5.0 is roughly "five robust standard deviations from the median".
    history_spread_floor_frac : float
        Floor on the robust scale as a fraction of the median: the effective
        scale is ``max(MAD/0.6745, history_spread_floor_frac * |median|)``.
        Stops a tight history from producing a hair-trigger z-score.
    history_ratio_band : float
        Magnitude fallback: the new estimate is an outlier when it is outside
        ``[median/band, median*band]`` regardless of z-score. Catches gross
        changes (e.g. a drop to near-zero) that the floored z-score may miss.
    cross_method_band : float
        The new estimate and its cross-method counterpart are an outlier pair
        when ``max/min > cross_method_band``.
    """

    min_history: int = 3
    history_z_threshold: float = 5.0
    history_spread_floor_frac: float = 0.25
    history_ratio_band: float = 4.0
    cross_method_band: float = 2.0


@dataclass(frozen=True)
class AnomalyAssessment:
    """Outcome of evaluating one published estimate.

    Parameters
    ----------
    is_anomaly : bool
        True iff at least one check flagged the estimate.
    kinds : tuple[str, ...]
        Which checks fired — a subset of ``("historical", "cross_method")``.
    reason : str or None
        Human-readable explanation, suitable for the public "under review"
        note. None when ``is_anomaly`` is False.
    modified_z : float or None
        The robust modified z-score of the new estimate against history, or
        None when the historical check was skipped.
    history_ratio : float or None
        new_p50 / median(history), or None when the historical check was
        skipped.
    cross_method_ratio : float or None
        max(new, cross) / min(new, cross), or None when no cross-method
        estimate was supplied.
    """

    is_anomaly: bool
    kinds: tuple[str, ...]
    reason: str | None
    modified_z: float | None
    history_ratio: float | None
    cross_method_ratio: float | None


def evaluate_published_estimate(
    new_p50: float,
    history_p50s: Sequence[float],
    cross_method_p50: float | None = None,
    *,
    thresholds: AnomalyThresholds | None = None,
) -> AnomalyAssessment:
    """Assess whether *new_p50* is an outlier. Pure function, no side effects.

    Parameters
    ----------
    new_p50 : float
        The median (p50) of the newly published estimate, in tCO2e.
    history_p50s : Sequence[float]
        p50 values of the facility's prior *published* estimates, excluding
        this one. May be empty (no baseline).
    cross_method_p50 : float or None
        The p50 of the event's other-method estimate (FRP if the headline is
        inventory-based, or vice versa). None when only one method exists.
    thresholds : AnomalyThresholds or None
        Override the default thresholds.

    Returns
    -------
    AnomalyAssessment
    """
    t = thresholds or AnomalyThresholds()
    kinds: list[str] = []
    reasons: list[str] = []

    # --- Historical check (robust) ---
    modified_z: float | None = None
    history_ratio: float | None = None
    history = np.asarray(list(history_p50s), dtype=float)
    if history.size >= t.min_history:
        median = float(np.median(history))
        mad = float(np.median(np.abs(history - median)))
        if median != 0.0:
            history_ratio = new_p50 / median
        # Floored robust scale: a tight history cannot drive the z-score sky-high.
        scale = max(mad / _MAD_TO_SIGMA, t.history_spread_floor_frac * abs(median))
        if scale > 0.0:
            modified_z = abs(new_p50 - median) / scale

        z_flag = modified_z is not None and modified_z > t.history_z_threshold
        ratio_flag = history_ratio is not None and (
            history_ratio > t.history_ratio_band
            or history_ratio < 1.0 / t.history_ratio_band
        )
        if z_flag:
            kinds.append("historical")
            reasons.append(
                f"p50={new_p50:,.1f} tCO2e is {modified_z:.1f} robust SDs from the "
                f"facility median {median:,.1f} (threshold {t.history_z_threshold:.1f})"
            )
        elif ratio_flag:
            kinds.append("historical")
            reasons.append(
                f"p50={new_p50:,.1f} tCO2e is {history_ratio:.2f}x the facility "
                f"baseline {median:,.1f} (band {t.history_ratio_band:.1f}x)"
            )

    # --- Cross-method check ---
    cross_method_ratio: float | None = None
    if cross_method_p50 is not None and new_p50 > 0.0 and cross_method_p50 > 0.0:
        hi = max(new_p50, cross_method_p50)
        lo = min(new_p50, cross_method_p50)
        cross_method_ratio = hi / lo
        if cross_method_ratio > t.cross_method_band:
            kinds.append("cross_method")
            reasons.append(
                f"headline p50={new_p50:,.1f} tCO2e diverges {cross_method_ratio:.1f}x "
                f"from the cross-method estimate {cross_method_p50:,.1f} "
                f"(band {t.cross_method_band:.1f}x)"
            )

    is_anomaly = bool(kinds)
    reason = "; ".join(reasons) if is_anomaly else None
    return AnomalyAssessment(
        is_anomaly=is_anomaly,
        kinds=tuple(kinds),
        reason=reason,
        modified_z=modified_z,
        history_ratio=history_ratio,
        cross_method_ratio=cross_method_ratio,
    )


def _p50_of(estimate: "Distribution | float | None") -> float | None:
    """Extract a p50 from a Distribution, a bare float, or None."""
    if estimate is None:
        return None
    if isinstance(estimate, (int, float)):
        return float(estimate)
    return float(estimate.p50)


class AnomalyWatch:
    """Monitors published estimates and auto-retracts outliers.

    The watch is constructed with a review queue; ``review`` evaluates one
    estimate and, when it is an outlier *and* the event is currently PUBLISHED,
    calls ``queue.flag_anomaly`` to return it to PENDING_REVIEW with a public
    "under review" note.
    """

    def __init__(
        self,
        queue: "ReviewQueueProtocol",
        *,
        thresholds: AnomalyThresholds | None = None,
        reviewer: str = "anomaly-watch",
    ) -> None:
        self._queue = queue
        self._thresholds = thresholds or AnomalyThresholds()
        self._reviewer = reviewer

    def review(
        self,
        event: FireEvent,
        distribution: "Distribution",
        *,
        facility_history: Sequence[float],
        cross_method_estimate: "Distribution | float | None" = None,
    ) -> AnomalyAssessment:
        """Evaluate one published estimate and auto-retract it if anomalous.

        Parameters
        ----------
        event : FireEvent
            The event whose estimate is being watched.
        distribution : Distribution
            The headline (published) emission estimate.
        facility_history : Sequence[float]
            p50 values of the facility's prior published estimates, excluding
            this event.
        cross_method_estimate : Distribution, float, or None
            The event's other-method estimate (FRP vs inventory), if available.

        Returns
        -------
        AnomalyAssessment
            The assessment. When ``is_anomaly`` is True and the event is
            PUBLISHED, the queue transition has already been performed.
        """
        assessment = evaluate_published_estimate(
            distribution.p50,
            facility_history,
            _p50_of(cross_method_estimate),
            thresholds=self._thresholds,
        )

        if not assessment.is_anomaly:
            return assessment

        if event.status is not EventStatus.PUBLISHED:
            # Nothing published to retract; record the finding without forcing
            # an invalid state transition.
            log.warning(
                "anomaly_watch.flagged_non_published event=%s status=%s reason=%r",
                event.id, event.status.value, assessment.reason,
            )
            return assessment

        self._queue.flag_anomaly(
            event.id,
            reviewer=self._reviewer,
            reason=assessment.reason or "outlier estimate flagged by anomaly-watch",
        )
        log.warning(
            "anomaly_watch.auto_retract event=%s kinds=%s reason=%r",
            event.id, assessment.kinds, assessment.reason,
        )
        return assessment
