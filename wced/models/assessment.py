"""Damage assessment data model.

A ``DamageAssessment`` records a human reviewer's estimate of the fraction
of a facility's inventory destroyed by a fire event. This is the ψ parameter
in methodology/v1.0.pdf §3.4 Eq. 5. It is NOT auto-computed — it is always
entered by a reviewer during the editorial approve workflow.

The (low, mode, high) triple defines a triangular distribution over ψ ∈ [0, 1],
which is sampled during Monte Carlo uncertainty propagation in
``wced.quantify.inventory``.
"""
from __future__ import annotations

import enum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class AssessmentMethod(str, enum.Enum):
    """How the reviewer arrived at the fraction_destroyed estimate."""

    SENTINEL2_VISUAL = "SENTINEL2_VISUAL"
    NEWS_REPORT = "NEWS_REPORT"
    EXPERT_ESTIMATE = "EXPERT_ESTIMATE"


class DamageAssessment(BaseModel):
    """A reviewer's estimate of fraction destroyed for one event/facility pair.

    Parameters
    ----------
    id : UUID
        Stable identifier for this assessment.
    event_id : UUID
        The FireEvent this assessment applies to.
    facility_id : UUID
        The Facility this assessment applies to.
    fraction_destroyed_low : float
        Lower bound of the triangular prior on ψ. Must be in [0, 1].
    fraction_destroyed_mode : float
        Mode (most likely value) of ψ. Must satisfy low <= mode <= high.
    fraction_destroyed_high : float
        Upper bound of ψ. Must be in [0, 1].
    assessed_by : str
        Reviewer identity (e.g. "analyst:jdoe").
    assessment_method : AssessmentMethod
        How the estimate was derived.
    notes : str or None
        Free-text explanation of the reasoning behind the estimate.
    assessed_at : AwareDatetime
        When the assessment was recorded (UTC).
    provenance_id : UUID
        ID of the ProvenanceRecord that wraps this assessment's source
        evidence (e.g. the Sentinel-2 scene used for visual inspection).
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    facility_id: UUID
    fraction_destroyed_low: float = Field(ge=0.0, le=1.0)
    fraction_destroyed_mode: float = Field(ge=0.0, le=1.0)
    fraction_destroyed_high: float = Field(ge=0.0, le=1.0)
    assessed_by: str = Field(min_length=1)
    assessment_method: AssessmentMethod
    notes: str | None = None
    assessed_at: AwareDatetime
    provenance_id: UUID

    @model_validator(mode="after")
    def _check_fraction_ordering(self) -> DamageAssessment:
        if not (self.fraction_destroyed_low
                <= self.fraction_destroyed_mode
                <= self.fraction_destroyed_high):
            raise ValueError(
                "fraction_destroyed must satisfy low <= mode <= high; "
                f"got ({self.fraction_destroyed_low}, "
                f"{self.fraction_destroyed_mode}, "
                f"{self.fraction_destroyed_high})"
            )
        return self

    @property
    def fraction_destroyed_pdf(self) -> tuple[float, float, float]:
        """Return the (low, mode, high) triple for ``compute_inventory_emissions``."""
        return (
            self.fraction_destroyed_low,
            self.fraction_destroyed_mode,
            self.fraction_destroyed_high,
        )
