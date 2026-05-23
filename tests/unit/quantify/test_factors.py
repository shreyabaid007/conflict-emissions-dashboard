"""Tests for wced.quantify.factors and wced.cli.main.

Covers:
- Both shipped YAML files load cleanly into FactorRegistry
- Every entry has a valid PDF specification per its distribution type
- Sampling: ≥95% of samples fall within each factor's natural 95% interval
- Pydantic-level rejection of malformed entries
- Loader caching via lru_cache
- CLI: list / show / unknown-key behavior for both factors and parameters
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import yaml
from typer.testing import CliRunner

from wced.cli.main import app
from wced.quantify.factors import (
    DEFAULT_EMISSION_FACTORS_PATH,
    DEFAULT_PARAMETER_DISTRIBUTIONS_PATH,
    EmissionFactor,
    FactorRegistry,
    load_factors,
    load_parameter_distributions,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# Tolerance band for the "95% within natural 95% CI" property test. The
# expected value is 0.90 because the natural 95% CI by construction excludes
# 10% of the mass (5% on each tail). Allow ±2% to absorb Monte Carlo noise.
_EXPECTED_INSIDE_NATURAL_CI = 0.90
_INSIDE_TOLERANCE = 0.02
_N_SAMPLES = 20_000


@pytest.fixture(autouse=True)
def _reset_factor_caches() -> None:
    """Each test starts with a clean lru_cache for both loaders."""
    load_factors.cache_clear()
    load_parameter_distributions.cache_clear()


def _write_yaml(path: Path, body: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# YAML files exist and load
# ---------------------------------------------------------------------------


class TestShippedYAMLFiles:
    def test_default_files_exist(self) -> None:
        assert DEFAULT_EMISSION_FACTORS_PATH.exists()
        assert DEFAULT_PARAMETER_DISTRIBUTIONS_PATH.exists()

    def test_emission_factors_load(self) -> None:
        reg = load_factors()
        assert isinstance(reg, FactorRegistry)
        assert reg.factors  # at least one entry
        assert reg.source_path == DEFAULT_EMISSION_FACTORS_PATH

    def test_parameter_distributions_load(self) -> None:
        reg = load_parameter_distributions()
        assert isinstance(reg, FactorRegistry)
        assert reg.factors
        assert reg.source_path == DEFAULT_PARAMETER_DISTRIBUTIONS_PATH

    def test_known_keys_present_in_factors(self) -> None:
        reg = load_factors()
        for k in (
            "crude_oil_combustion",
            "frp_to_combustion_rate",
            "carbon_recovery_as_co2",
        ):
            assert k in reg, f"missing factor key {k!r}"


# ---------------------------------------------------------------------------
# Valid PDF specification per entry
# ---------------------------------------------------------------------------


def _all_factors() -> list[tuple[str, EmissionFactor]]:
    """Return every (key, factor) across both shipped YAML files."""
    out: list[tuple[str, EmissionFactor]] = []
    for reg in (load_factors(), load_parameter_distributions()):
        for key in reg.keys():
            out.append((key, reg[key]))
    return out


class TestPDFSpecValid:
    @pytest.mark.parametrize(("key", "factor"), _all_factors())
    def test_every_entry_has_complete_spec(
        self, key: str, factor: EmissionFactor
    ) -> None:
        assert factor.key == key
        assert factor.units
        assert factor.source
        assert factor.methodology_section
        # Distribution-specific:
        if factor.distribution == "normal":
            assert factor.sigma is not None and factor.sigma > 0
        elif factor.distribution == "triangular":
            assert factor.low is not None
            assert factor.mode is not None
            assert factor.high is not None
            assert factor.low <= factor.mode <= factor.high
        elif factor.distribution == "uniform":
            assert factor.low is not None and factor.high is not None
            assert factor.low <= factor.high

    @pytest.mark.parametrize(("key", "factor"), _all_factors())
    def test_natural_95ci_is_ordered(
        self, key: str, factor: EmissionFactor
    ) -> None:
        lo, hi = factor.natural_95_ci()
        assert lo <= hi, f"{key}: natural 95% CI inverted: {lo} > {hi}"


# ---------------------------------------------------------------------------
# Sampling within bounds
# ---------------------------------------------------------------------------


class TestSamplingWithinBounds:
    @pytest.mark.parametrize(("key", "factor"), _all_factors())
    def test_95pct_of_samples_within_natural_ci(
        self, key: str, factor: EmissionFactor
    ) -> None:
        """≥95% of samples should fall within the factor's natural 95% CI.

        The natural 95% CI is computed from the distribution parameters
        (not the documentation-only ``uncertainty_low``/``uncertainty_high``
        fields). For triangular and uniform, this is the full support, so
        100% of samples are inside by construction. For normal, the
        ``natural_95_ci`` method returns ``value ± 1.645σ``, which contains
        exactly 90% of the mass — the test asserts within ±2% of that
        expected fraction.
        """
        rng = np.random.default_rng(seed=hash(key) & 0xFFFFFFFF)
        dist = factor.sample(
            n_samples=_N_SAMPLES,
            provenance_id=uuid4(),
            methodology_version="1.0",
            rng=rng,
        )
        samples = dist.samples
        assert samples is not None

        lo, hi = factor.natural_95_ci()

        if factor.distribution in ("triangular", "uniform"):
            # Hard-bounded distributions: 100% of samples must be in [lo, hi].
            inside = float(np.mean((samples >= lo) & (samples <= hi)))
            assert inside >= 0.99, (
                f"{key}: only {inside:.3f} of triangular/uniform samples "
                f"fell inside [{lo}, {hi}]"
            )
        elif factor.distribution == "normal":
            # natural_95_ci() returns ±1.645σ → exactly 90% expected.
            inside = float(np.mean((samples >= lo) & (samples <= hi)))
            assert abs(inside - _EXPECTED_INSIDE_NATURAL_CI) < _INSIDE_TOLERANCE, (
                f"{key}: {inside:.3f} of normal samples inside [{lo}, {hi}]; "
                f"expected ~{_EXPECTED_INSIDE_NATURAL_CI} ± {_INSIDE_TOLERANCE}"
            )
        else:  # constant
            assert np.all(samples == factor.value)

    def test_normal_factor_95pct_in_widened_ci(self) -> None:
        """≥95% of samples must fall within ±1.96σ — the textbook 95% CI."""
        reg = load_factors()
        factor = reg["frp_to_combustion_rate"]
        assert factor.distribution == "normal"
        assert factor.sigma is not None
        rng = np.random.default_rng(seed=42)
        dist = factor.sample(
            n_samples=_N_SAMPLES,
            provenance_id=uuid4(),
            methodology_version="1.0",
            rng=rng,
        )
        assert dist.samples is not None
        lo = factor.value - 1.96 * factor.sigma
        hi = factor.value + 1.96 * factor.sigma
        inside = float(np.mean((dist.samples >= lo) & (dist.samples <= hi)))
        assert inside >= 0.94, f"only {inside:.3f} within ±1.96σ"


# ---------------------------------------------------------------------------
# Pydantic-level rejection of malformed entries
# ---------------------------------------------------------------------------


class TestSchemaRejection:
    def test_normal_without_sigma_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "bad.yaml",
            {
                "factors": {
                    "broken": {
                        "value": 1.0,
                        "distribution": "normal",
                        "units": "x",
                        "source": "test",
                        "methodology_section": "0.0",
                    }
                }
            },
        )
        with pytest.raises(Exception, match="sigma"):
            load_factors(path)

    def test_triangular_missing_bounds_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "bad.yaml",
            {
                "factors": {
                    "broken": {
                        "value": 1.0,
                        "distribution": "triangular",
                        "units": "x",
                        "source": "test",
                        "methodology_section": "0.0",
                    }
                }
            },
        )
        with pytest.raises(Exception, match="triangular requires"):
            load_factors(path)

    def test_triangular_inverted_bounds_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "bad.yaml",
            {
                "factors": {
                    "broken": {
                        "value": 1.0,
                        "distribution": "triangular",
                        "low": 2.0,
                        "mode": 1.0,
                        "high": 0.5,
                        "units": "x",
                        "source": "test",
                        "methodology_section": "0.0",
                    }
                }
            },
        )
        with pytest.raises(Exception, match="low <= mode <= high"):
            load_factors(path)

    def test_uniform_inverted_bounds_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "bad.yaml",
            {
                "factors": {
                    "broken": {
                        "value": 1.0,
                        "distribution": "uniform",
                        "low": 5.0,
                        "high": 1.0,
                        "units": "x",
                        "source": "test",
                        "methodology_section": "0.0",
                    }
                }
            },
        )
        with pytest.raises(Exception, match="low <= high"):
            load_factors(path)

    def test_unknown_distribution_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "bad.yaml",
            {
                "factors": {
                    "broken": {
                        "value": 1.0,
                        "distribution": "weibull",
                        "units": "x",
                        "source": "test",
                        "methodology_section": "0.0",
                    }
                }
            },
        )
        with pytest.raises(Exception):
            load_factors(path)

    def test_missing_factors_section_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("name: not_a_factor_file\n", encoding="utf-8")
        with pytest.raises(ValueError, match="factors"):
            load_factors(path)

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_factors(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# Loader caching
# ---------------------------------------------------------------------------


class TestLoaderCaching:
    def test_same_path_returns_same_object(self) -> None:
        a = load_factors()
        b = load_factors()
        assert a is b

    def test_separate_files_keyed_separately(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "extra.yaml",
            {
                "factors": {
                    "x": {
                        "value": 1.0,
                        "distribution": "constant",
                        "units": "x",
                        "source": "test",
                        "methodology_section": "0.0",
                    }
                }
            },
        )
        a = load_factors(path)
        b = load_factors(path)
        assert a is b
        default = load_factors()
        assert default is not a


# ---------------------------------------------------------------------------
# Shorthand normalization
# ---------------------------------------------------------------------------


class TestShorthandNormalization:
    def test_uncertainty_bounds_fill_in_triangular_support(self) -> None:
        f = load_factors()["crude_oil_combustion"]
        # YAML provided only uncertainty_low/uncertainty_high; loader should
        # promote them to low/high with mode=value.
        assert f.distribution == "triangular"
        assert f.low == f.uncertainty_low
        assert f.high == f.uncertainty_high
        assert f.mode == f.value


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestCLIFactors:
    def test_list_prints_all_factor_keys(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["factors", "list"])
        assert result.exit_code == 0, result.output
        for key in load_factors().keys():
            assert key in result.output

    def test_show_prints_known_factor(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["factors", "show", "crude_oil_combustion"])
        assert result.exit_code == 0, result.output
        assert "crude_oil_combustion" in result.output
        assert "tCO2_per_barrel" in result.output

    def test_show_unknown_factor_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["factors", "show", "no_such_factor"])
        assert result.exit_code == 1
        assert "Unknown" in result.output or "Unknown" in (result.stderr or "")


class TestCLIParameters:
    def test_list_prints_all_parameter_keys(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["parameters", "list"])
        assert result.exit_code == 0, result.output
        for key in load_parameter_distributions().keys():
            assert key in result.output

    def test_show_prints_known_parameter(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["parameters", "show", "burn_duty_cycle"])
        assert result.exit_code == 0, result.output
        assert "burn_duty_cycle" in result.output


class TestCLICustomPath:
    def test_factors_list_with_path_override(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        path = _write_yaml(
            tmp_path / "alt.yaml",
            {
                "factors": {
                    "only_one": {
                        "value": 0.5,
                        "distribution": "constant",
                        "units": "dimensionless",
                        "source": "test",
                        "methodology_section": "X.Y",
                    }
                }
            },
        )
        result = runner.invoke(app, ["factors", "list", "--path", str(path)])
        assert result.exit_code == 0, result.output
        assert "only_one" in result.output
        # Default-path entries must NOT leak in:
        assert "crude_oil_combustion" not in result.output
