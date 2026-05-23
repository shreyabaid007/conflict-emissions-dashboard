"""Emission factor and parameter distribution loaders.

This module loads ``data/emission_factors.yaml`` (per-fuel emission
coefficients) and ``data/parameter_distributions.yaml`` (Monte Carlo priors
for event- and facility-level assumptions). Both files share a single
schema so the same loader and Pydantic models cover them.

The active distribution-defining fields per ``distribution`` type are:

- ``triangular`` : ``low``, ``mode``, ``high``. When omitted, ``low`` and
  ``high`` default to ``uncertainty_low``/``uncertainty_high`` and ``mode``
  defaults to ``value`` — this matches the shorthand used by editors.
- ``normal``     : ``sigma`` (linear units); mean = ``value``.
- ``uniform``    : ``low``, ``high``.
- ``constant``   : no extra params; degenerate point distribution.

``uncertainty_low``/``uncertainty_high`` are always optional documentation
of the source paper's reported envelope. They are never used to override
distribution parameters at sampling time.

Methodology reference: methodology/v1.0.pdf §2.1 — "Parameter Priors and
Emission Factors".
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from wced.quantify.distribution import Distribution

__all__ = [
    "DEFAULT_EMISSION_FACTORS_PATH",
    "DEFAULT_PARAMETER_DISTRIBUTIONS_PATH",
    "DistributionKind",
    "EmissionFactor",
    "FactorRegistry",
    "load_factors",
    "load_parameter_distributions",
]


DistributionKind = Literal["triangular", "normal", "uniform", "constant"]


def _repo_root() -> Path:
    """Repo root, assuming this file lives at <root>/wced/quantify/factors.py."""
    return Path(__file__).resolve().parents[2]


DEFAULT_EMISSION_FACTORS_PATH: Path = _repo_root() / "data" / "emission_factors.yaml"
DEFAULT_PARAMETER_DISTRIBUTIONS_PATH: Path = (
    _repo_root() / "data" / "parameter_distributions.yaml"
)


class EmissionFactor(BaseModel):
    """A single factor entry: a value, a distribution, and provenance metadata.

    Parameters
    ----------
    key : str
        Lookup name (the YAML mapping key). Injected by the loader.
    value : float
        Central estimate. Mean for ``normal``; mode for ``triangular``;
        point value for ``constant``; midpoint reference for ``uniform``.
    units : str
        Physical unit string consumed by callers, e.g. ``"tCO2_per_barrel"``.
    source : str
        Citation for the value. Must trace to an archived bibliography entry.
    methodology_section : str
        Section number in methodology/v1.0.pdf justifying this factor.
    distribution : DistributionKind
        How to sample. Defaults to ``"triangular"`` when not specified.
    sigma : float or None
        Required for ``normal``. Standard deviation in linear units.
    low, mode, high : float or None
        Required for ``triangular``. ``low`` and ``high`` also for
        ``uniform``. Loader fills in defaults from ``value`` and
        ``uncertainty_low``/``uncertainty_high`` when omitted.
    uncertainty_low, uncertainty_high : float or None
        Source-reported confidence envelope. Documentation only — not used
        for sampling once the distribution has been resolved.
    notes : str or None
        Caveats, calibration history, interpretation guidance.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    value: float
    units: str = Field(min_length=1)
    source: str = Field(min_length=1)
    methodology_section: str = Field(min_length=1)
    distribution: DistributionKind = "triangular"

    sigma: float | None = None
    low: float | None = None
    mode: float | None = None
    high: float | None = None

    uncertainty_low: float | None = None
    uncertainty_high: float | None = None

    notes: str | None = None

    # ------------------------------------------------------------------
    # Normalization + validation
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _fill_defaults_from_shorthand(cls, data: Any) -> Any:
        """Populate omitted distribution params from value + uncertainty_*.

        The YAML allows editors to write ``value`` + ``uncertainty_low`` +
        ``uncertainty_high`` and implicitly mean "triangular with those as
        the support and ``value`` as the mode". This validator makes that
        shorthand explicit so the rest of the code can rely on ``low``,
        ``mode``, ``high`` being populated whenever ``distribution`` is
        ``"triangular"``.
        """
        if not isinstance(data, dict):
            return data

        dist = data.get("distribution", "triangular")
        if dist == "triangular":
            if data.get("low") is None and data.get("uncertainty_low") is not None:
                data["low"] = data["uncertainty_low"]
            if data.get("high") is None and data.get("uncertainty_high") is not None:
                data["high"] = data["uncertainty_high"]
            if data.get("mode") is None and data.get("value") is not None:
                data["mode"] = data["value"]
        elif dist == "uniform":
            if data.get("low") is None and data.get("uncertainty_low") is not None:
                data["low"] = data["uncertainty_low"]
            if data.get("high") is None and data.get("uncertainty_high") is not None:
                data["high"] = data["uncertainty_high"]

        return data

    @model_validator(mode="after")
    def _check_distribution_params(self) -> EmissionFactor:
        key = self.key
        if self.distribution == "normal":
            if self.sigma is None:
                raise ValueError(f"factor {key!r}: normal distribution requires sigma")
            if self.sigma <= 0:
                raise ValueError(
                    f"factor {key!r}: sigma must be > 0; got {self.sigma}"
                )
        elif self.distribution == "triangular":
            missing = [
                name for name, v in (("low", self.low), ("mode", self.mode), ("high", self.high))
                if v is None
            ]
            if missing:
                raise ValueError(
                    f"factor {key!r}: triangular requires {missing} "
                    f"(supply directly or via uncertainty_low/uncertainty_high)"
                )
            if not (self.low <= self.mode <= self.high):  # type: ignore[operator]
                raise ValueError(
                    f"factor {key!r}: triangular requires low <= mode <= high; "
                    f"got ({self.low}, {self.mode}, {self.high})"
                )
        elif self.distribution == "uniform":
            if self.low is None or self.high is None:
                raise ValueError(
                    f"factor {key!r}: uniform requires low and high"
                )
            if self.low > self.high:
                raise ValueError(
                    f"factor {key!r}: uniform requires low <= high; "
                    f"got ({self.low}, {self.high})"
                )
        elif self.distribution == "constant":
            # Nothing to validate beyond `value`.
            pass

        # Documentation-only fields: ordering check when both supplied.
        if (
            self.uncertainty_low is not None
            and self.uncertainty_high is not None
            and self.uncertainty_low > self.uncertainty_high
        ):
            raise ValueError(
                f"factor {key!r}: uncertainty_low must be <= uncertainty_high"
            )

        return self

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------

    def sample(
        self,
        n_samples: int,
        provenance_id: UUID,
        methodology_version: str,
        rng: np.random.Generator | None = None,
    ) -> Distribution:
        """Draw ``n_samples`` from this factor's distribution.

        Parameters
        ----------
        n_samples : int
            Number of Monte Carlo draws.
        provenance_id : UUID
            ID of the ProvenanceRecord that wraps this factor's load. Stored
            on the returned Distribution so downstream arithmetic traces
            back to the YAML entry.
        methodology_version : str
            Semver string of the methodology PDF that approves these values.
        rng : np.random.Generator, optional
            Seeded generator for reproducibility. A fresh unseeded generator
            is used if not provided.
        """
        _rng = rng if rng is not None else np.random.default_rng()
        if self.distribution == "normal":
            assert self.sigma is not None  # validated above
            return Distribution.from_normal(
                mean=self.value,
                std=self.sigma,
                n_samples=n_samples,
                units=self.units,
                methodology_version=methodology_version,
                provenance_id=provenance_id,
                rng=_rng,
            )
        if self.distribution == "triangular":
            assert self.low is not None and self.mode is not None and self.high is not None
            return Distribution.from_triangular(
                low=self.low,
                mode=self.mode,
                high=self.high,
                n_samples=n_samples,
                units=self.units,
                methodology_version=methodology_version,
                provenance_id=provenance_id,
                rng=_rng,
            )
        if self.distribution == "uniform":
            assert self.low is not None and self.high is not None
            return Distribution.from_uniform(
                low=self.low,
                high=self.high,
                n_samples=n_samples,
                units=self.units,
                methodology_version=methodology_version,
                provenance_id=provenance_id,
                rng=_rng,
            )
        # constant
        return Distribution.constant(
            value=self.value,
            units=self.units,
            methodology_version=methodology_version,
            provenance_id=provenance_id,
        )

    def natural_95_ci(self) -> tuple[float, float]:
        """Return the distribution's natural 5th–95th percentile bounds.

        Used by editorial review and the sampling-bounds test. Distinct
        from ``uncertainty_low``/``uncertainty_high`` which document the
        source paper's reported range.
        """
        if self.distribution == "normal":
            assert self.sigma is not None
            # ~1.645σ — exact z for 5/95
            z = 1.6448536269514722
            return (self.value - z * self.sigma, self.value + z * self.sigma)
        if self.distribution == "triangular":
            assert self.low is not None and self.high is not None
            return (self.low, self.high)
        if self.distribution == "uniform":
            assert self.low is not None and self.high is not None
            return (self.low, self.high)
        # constant
        return (self.value, self.value)


class FactorRegistry(BaseModel):
    """Typed view of a loaded factor YAML file.

    Parameters
    ----------
    factors : dict[str, EmissionFactor]
        Mapping from factor key to validated EmissionFactor. Keys match the
        YAML mapping keys verbatim.
    source_path : Path
        Path the registry was loaded from. Useful for audit logs and
        editorial CLI output.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    factors: dict[str, EmissionFactor]
    source_path: Path

    def __getitem__(self, key: str) -> EmissionFactor:
        try:
            return self.factors[key]
        except KeyError as exc:
            raise KeyError(
                f"Unknown factor {key!r}; known keys: {sorted(self.factors)}"
            ) from exc

    def __contains__(self, key: object) -> bool:
        return key in self.factors

    def keys(self) -> list[str]:
        return list(self.factors.keys())


def _load_registry(path: Path) -> FactorRegistry:
    """Parse a YAML file at ``path`` into a FactorRegistry."""
    if not path.exists():
        raise FileNotFoundError(f"factor file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: top-level YAML must be a mapping with a 'factors' key"
        )
    factors_raw = raw.get("factors")
    if not isinstance(factors_raw, dict):
        raise ValueError(f"{path}: missing or non-mapping 'factors' section")

    parsed: dict[str, EmissionFactor] = {}
    for key, entry in factors_raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: factor {key!r} must be a mapping")
        parsed[key] = EmissionFactor(key=key, **entry)
    return FactorRegistry(factors=parsed, source_path=path)


@lru_cache(maxsize=8)
def load_factors(path: Path | None = None) -> FactorRegistry:
    """Load and cache emission factors from a YAML file.

    Parameters
    ----------
    path : Path or None
        File to load. Defaults to ``DEFAULT_EMISSION_FACTORS_PATH``.

    Notes
    -----
    Cached via ``functools.lru_cache``. To pick up edits to the YAML during
    a long-running process, call ``load_factors.cache_clear()`` first.
    """
    return _load_registry(path or DEFAULT_EMISSION_FACTORS_PATH)


@lru_cache(maxsize=8)
def load_parameter_distributions(path: Path | None = None) -> FactorRegistry:
    """Load and cache Monte Carlo parameter priors from a YAML file.

    Parameters
    ----------
    path : Path or None
        File to load. Defaults to ``DEFAULT_PARAMETER_DISTRIBUTIONS_PATH``.

    Notes
    -----
    The schema is identical to ``load_factors``; the two functions exist
    separately so caching is keyed per-file and callers signal intent.
    """
    return _load_registry(path or DEFAULT_PARAMETER_DISTRIBUTIONS_PATH)
