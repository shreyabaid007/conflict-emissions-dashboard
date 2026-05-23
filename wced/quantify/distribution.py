"""Distribution — the central numeric type for WCED.

Every emission estimate is a Distribution, never a float. All arithmetic on
Distributions propagates uncertainty through the pipeline by operating on the
underlying Monte Carlo sample arrays element-wise.

Placeholder reference: methodology/v1.0.pdf §2.1 — "Probability Distributions
for Emission Estimates". (PDF pending Scientific Steering Committee approval.)

Design notes
------------
- Samples are optional on the model so large arrays can be dropped before
  persisting to the database; the p5/p50/p95/mean/std fields are always
  retained. Re-adding samples requires re-running the Monte Carlo step.
- Arithmetic operations require both operands to carry samples and to have
  the same methodology_version. A version mismatch is an error, not a warning,
  because mixing methodology versions silently would corrupt the audit trail.
- provenance_id on a derived Distribution is a deterministic UUID derived from
  the two parent IDs (uuid5 in a private namespace). The provenance module
  later replaces this with a proper ProvenanceRecord link.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal, Optional
from uuid import UUID

import numpy as np
from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

# Private namespace UUID for deriving combined provenance IDs.
# Stable across runs so the same pair always produces the same derived ID.
_PROVENANCE_NS = uuid.UUID("b1d2e3f4-0000-5000-8000-000000000001")
_PROVENANCE_COMBINE_SENTINEL = "⊕"  # ⊕  marks a derived (combined) provenance ID


def _combine_provenance_ids(a: UUID, b: UUID) -> UUID:
    """Return a deterministic UUID representing the combination of two provenance IDs.

    Uses uuid5 so the same pair always produces the same derived ID.
    The order of operands matters (a⊕b ≠ b⊕a) so arithmetic direction is
    preserved in the audit trail.
    """
    return uuid.uuid5(_PROVENANCE_NS, f"{a}{_PROVENANCE_COMBINE_SENTINEL}{b}")


class Distribution(BaseModel):
    """A probability distribution over an emission estimate.

    All numeric emission outputs in WCED are Distribution objects, never plain
    floats. See methodology/v1.0.pdf §2.1 for the full specification.

    Parameters
    ----------
    samples : np.ndarray or None
        1-D array of Monte Carlo samples in *units*. May be None when samples
        are stripped for storage. Arithmetic requires samples to be present.
    p5, p50, p95 : float
        5th, 50th (median), and 95th percentiles in *units*. Always present.
        Invariant: p5 <= p50 <= p95.
    mean : float
        Arithmetic mean of *samples* in *units*.
    std : float
        Population standard deviation of *samples* in *units*.
    units : str
        Physical unit string. Emission estimates always use "tCO2e".
    methodology_version : str
        Semantic-version string of the methodology PDF (e.g. "1.0").
    provenance_id : UUID
        ID of the ProvenanceRecord that produced this distribution.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    samples: Optional[np.ndarray] = None
    p5: float
    p50: float
    p95: float
    mean: float
    std: float
    units: str
    methodology_version: str
    provenance_id: UUID

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("samples", mode="before")
    @classmethod
    def _coerce_samples(cls, v: Any) -> Optional[np.ndarray]:
        """Accept list[float] from JSON deserialization; pass ndarray through."""
        if v is None:
            return None
        if isinstance(v, np.ndarray):
            return v.astype(float)
        return np.asarray(v, dtype=float)

    @model_validator(mode="after")
    def _check_percentile_order(self) -> "Distribution":
        """Enforce p5 <= p50 <= p95. See methodology/v1.0.pdf §2.1."""
        if not (self.p5 <= self.p50 <= self.p95):
            raise ValueError(
                f"Percentile invariant violated: "
                f"p5={self.p5} <= p50={self.p50} <= p95={self.p95} must hold"
            )
        return self

    # ------------------------------------------------------------------
    # Serializers
    # ------------------------------------------------------------------

    @field_serializer("samples")
    def _serialize_samples(self, v: Optional[np.ndarray]) -> Optional[list[float]]:
        """Serialize ndarray as a plain list for JSON round-trips."""
        return v.tolist() if v is not None else None

    @field_serializer("provenance_id")
    def _serialize_provenance_id(self, v: UUID) -> str:
        return str(v)

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_samples(
        cls,
        samples: np.ndarray,
        units: str,
        methodology_version: str,
        provenance_id: UUID,
    ) -> "Distribution":
        """Build a Distribution by computing statistics from raw MC samples.

        Parameters
        ----------
        samples : np.ndarray
            1-D array of values in *units*.
        units : str
            Physical unit string (e.g. "tCO2e").
        methodology_version : str
            Semver string matching the methodology PDF used to produce these
            samples.
        provenance_id : UUID
            ID of the upstream ProvenanceRecord.
        """
        s = np.asarray(samples, dtype=float)
        if s.ndim != 1:
            raise ValueError(f"samples must be 1-D; got shape {s.shape}")
        if len(s) == 0:
            raise ValueError("samples must be non-empty")
        return cls(
            samples=s,
            p5=float(np.percentile(s, 5)),
            p50=float(np.percentile(s, 50)),
            p95=float(np.percentile(s, 95)),
            mean=float(np.mean(s)),
            std=float(np.std(s, ddof=0)),
            units=units,
            methodology_version=methodology_version,
            provenance_id=provenance_id,
        )

    @classmethod
    def from_normal(
        cls,
        mean: float,
        std: float,
        n_samples: int,
        units: str,
        methodology_version: str,
        provenance_id: UUID,
        rng: Optional[np.random.Generator] = None,
    ) -> "Distribution":
        """Sample from N(mean, std²).

        Parameters
        ----------
        mean : float
            Distribution mean in *units*.
        std : float
            Standard deviation in *units*. Must be >= 0.
        n_samples : int
            Number of Monte Carlo draws. Must be >= 1.
        rng : np.random.Generator, optional
            Seeded generator for reproducibility. A fresh unseeded generator is
            used if not provided — store the seed in the ProvenanceRecord.
        """
        if std < 0:
            raise ValueError(f"std must be >= 0; got {std}")
        if n_samples < 1:
            raise ValueError(f"n_samples must be >= 1; got {n_samples}")
        _rng = rng if rng is not None else np.random.default_rng()
        return cls.from_samples(
            _rng.normal(mean, std, n_samples), units, methodology_version, provenance_id
        )

    @classmethod
    def from_triangular(
        cls,
        low: float,
        mode: float,
        high: float,
        n_samples: int,
        units: str,
        methodology_version: str,
        provenance_id: UUID,
        rng: Optional[np.random.Generator] = None,
    ) -> "Distribution":
        """Sample from Triangular(low, mode, high).

        Parameters
        ----------
        low : float
            Lower bound in *units*.
        mode : float
            Peak (most likely value) in *units*.
        high : float
            Upper bound in *units*.

        Raises
        ------
        ValueError
            If low > mode or mode > high.
        """
        if not (low <= mode <= high):
            raise ValueError(
                f"Triangular requires low <= mode <= high; got ({low}, {mode}, {high})"
            )
        # numpy raises when low == high; degenerate case is a constant.
        if low == high:
            return cls.constant(low, units, methodology_version, provenance_id)
        _rng = rng if rng is not None else np.random.default_rng()
        return cls.from_samples(
            _rng.triangular(low, mode, high, n_samples),
            units,
            methodology_version,
            provenance_id,
        )

    @classmethod
    def from_lognormal(
        cls,
        mu: float,
        sigma: float,
        n_samples: int,
        units: str,
        methodology_version: str,
        provenance_id: UUID,
        rng: Optional[np.random.Generator] = None,
    ) -> "Distribution":
        """Sample from LogNormal(mu, sigma²) where mu and sigma are log-space parameters.

        The arithmetic mean of the resulting distribution is exp(mu + sigma²/2).

        Parameters
        ----------
        mu : float
            Mean of the underlying normal distribution (log-space).
        sigma : float
            Standard deviation of the underlying normal (log-space). Must be > 0.
        """
        if sigma <= 0:
            raise ValueError(f"sigma must be > 0 for lognormal; got {sigma}")
        if n_samples < 1:
            raise ValueError(f"n_samples must be >= 1; got {n_samples}")
        _rng = rng if rng is not None else np.random.default_rng()
        return cls.from_samples(
            _rng.lognormal(mu, sigma, n_samples), units, methodology_version, provenance_id
        )

    @classmethod
    def from_uniform(
        cls,
        low: float,
        high: float,
        n_samples: int,
        units: str,
        methodology_version: str,
        provenance_id: UUID,
        rng: Optional[np.random.Generator] = None,
    ) -> "Distribution":
        """Sample from Uniform(low, high).

        Parameters
        ----------
        low : float
            Lower bound in *units*.
        high : float
            Upper bound in *units*. Must be >= low.
        """
        if high < low:
            raise ValueError(f"high must be >= low; got low={low}, high={high}")
        if n_samples < 1:
            raise ValueError(f"n_samples must be >= 1; got {n_samples}")
        _rng = rng if rng is not None else np.random.default_rng()
        return cls.from_samples(
            _rng.uniform(low, high, n_samples), units, methodology_version, provenance_id
        )

    @classmethod
    def constant(
        cls,
        value: float,
        units: str,
        methodology_version: str,
        provenance_id: UUID,
    ) -> "Distribution":
        """Degenerate distribution for a known constant value (zero uncertainty).

        Useful for multiplication by a fixed emission factor when the factor
        itself carries no distributional uncertainty in the current methodology
        version. See methodology/v1.0.pdf §2.1.

        Parameters
        ----------
        value : float
            The certain value in *units*.
        """
        return cls(
            samples=np.array([value], dtype=float),
            p5=value,
            p50=value,
            p95=value,
            mean=value,
            std=0.0,
            units=units,
            methodology_version=methodology_version,
            provenance_id=provenance_id,
        )

    # ------------------------------------------------------------------
    # Arithmetic helpers
    # ------------------------------------------------------------------

    def _require_samples(self) -> np.ndarray:
        if self.samples is None:
            raise ValueError(
                "Arithmetic requires samples to be present. "
                "Re-construct this Distribution using a from_* class method."
            )
        return self.samples

    def _check_compatible(self, other: "Distribution") -> None:
        """Raise if the two Distributions cannot be combined.

        Mixing methodology versions corrupts the audit trail; same-length
        samples are required for element-wise arithmetic.
        """
        if self.methodology_version != other.methodology_version:
            raise ValueError(
                f"Cannot combine Distributions from different methodology versions: "
                f"{self.methodology_version!r} vs {other.methodology_version!r}"
            )
        s_self = self._require_samples()
        s_other = other._require_samples()
        if len(s_self) != len(s_other):
            raise ValueError(
                f"Cannot combine Distributions with different sample counts: "
                f"{len(s_self)} vs {len(s_other)}"
            )

    def _apply_op(self, other: "Distribution | float | int", op: str) -> "Distribution":
        """Core dispatch for all binary arithmetic."""
        s_self = self._require_samples()

        if isinstance(other, Distribution):
            self._check_compatible(other)
            s_other = other._require_samples()
            new_provenance = _combine_provenance_ids(self.provenance_id, other.provenance_id)
            ops = {
                "add": s_self + s_other,
                "sub": s_self - s_other,
                "mul": s_self * s_other,
                "div": s_self / s_other,
            }
        elif isinstance(other, (int, float)):
            # Derive a new provenance ID encoding the parent, op, and scalar.
            # This records that a transformation occurred without inheriting the
            # parent's ID, which would hide the step from the audit trail.
            # When the scalar comes from a known source (e.g. an emission factor
            # from emission_factors.yaml), use apply_scalar() instead to attach
            # the factor's own provenance_id explicitly.
            scalar = float(other)
            new_provenance = uuid.uuid5(
                _PROVENANCE_NS, f"{self.provenance_id}:{op}:{scalar}"
            )
            ops = {
                "add": s_self + scalar,
                "sub": s_self - scalar,
                "mul": s_self * scalar,
                "div": s_self / scalar,
            }
        else:
            return NotImplemented  # type: ignore[return-value]

        if op not in ops:
            raise ValueError(f"Unknown arithmetic op: {op!r}")

        return Distribution.from_samples(
            ops[op],
            units=self.units,
            methodology_version=self.methodology_version,
            provenance_id=new_provenance,
        )

    def __add__(self, other: "Distribution | float | int") -> "Distribution":
        return self._apply_op(other, "add")

    def __radd__(self, other: "float | int") -> "Distribution":
        return self._apply_op(other, "add")

    def __sub__(self, other: "Distribution | float | int") -> "Distribution":
        return self._apply_op(other, "sub")

    def __rsub__(self, other: "float | int") -> "Distribution":
        s_self = self._require_samples()
        scalar = float(other)
        new_provenance = uuid.uuid5(
            _PROVENANCE_NS, f"{self.provenance_id}:rsub:{scalar}"
        )
        return Distribution.from_samples(
            scalar - s_self,
            units=self.units,
            methodology_version=self.methodology_version,
            provenance_id=new_provenance,
        )

    def __mul__(self, other: "Distribution | float | int") -> "Distribution":
        return self._apply_op(other, "mul")

    def __rmul__(self, other: "float | int") -> "Distribution":
        return self._apply_op(other, "mul")

    def __truediv__(self, other: "Distribution | float | int") -> "Distribution":
        return self._apply_op(other, "div")

    def apply_scalar(
        self,
        scalar: float,
        op: Literal["mul", "div", "add", "sub"],
        provenance_id: UUID,
    ) -> "Distribution":
        """Apply a scalar with an explicitly supplied source provenance_id.

        Use this instead of the arithmetic operators whenever the scalar
        originates from a known source — specifically, any factor loaded from
        ``data/emission_factors.yaml``. The provenance_id here should be the
        id of the ProvenanceRecord or Source that defines the scalar value,
        so the audit trail can reach back to the YAML entry and its citation.

        Example (in wced/quantify/frp.py)::

            co2_dist = fuel_dist.apply_scalar(
                co2_per_kg,
                op="mul",
                provenance_id=factors_record.id,
            )
        """
        s = self._require_samples()
        result_map: dict[str, np.ndarray] = {
            "mul": s * scalar,
            "div": s / scalar,
            "add": s + scalar,
            "sub": s - scalar,
        }
        if op not in result_map:
            raise ValueError(f"Unknown op for apply_scalar: {op!r}")
        return Distribution.from_samples(
            result_map[op],
            units=self.units,
            methodology_version=self.methodology_version,
            provenance_id=provenance_id,
        )

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        n = len(self.samples) if self.samples is not None else 0
        return (
            f"Distribution("
            f"p5={self.p5:.4g}, p50={self.p50:.4g}, p95={self.p95:.4g} "
            f"{self.units}, n={n}, v={self.methodology_version!r})"
        )

    def without_samples(self) -> "Distribution":
        """Return a copy with samples stripped for compact serialization.

        The p5/p50/p95/mean/std fields are retained. Use this before persisting
        a Distribution to the database to avoid storing large arrays.
        """
        return self.model_copy(update={"samples": None})
