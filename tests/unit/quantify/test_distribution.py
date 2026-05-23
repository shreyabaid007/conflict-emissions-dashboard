"""Tests for wced.quantify.distribution.

Hand-computed expected values are derived analytically from known distribution
parameters; see inline comments. Tests are grouped by construction path, then
arithmetic, then invariants, then the Hypothesis property suite.
"""
from __future__ import annotations

import json
import math
import uuid
from uuid import UUID

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from wced.quantify.distribution import Distribution, _combine_provenance_ids

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

VER = "1.0"
UNITS = "tCO2e"


def pid() -> UUID:
    """Return a fresh random provenance ID."""
    return uuid.uuid4()


def seeded_rng(seed: int = 42) -> np.random.Generator:
    return np.random.default_rng(seed)


# ---------------------------------------------------------------------------
# Construction — from_samples
# ---------------------------------------------------------------------------


class TestFromSamples:
    def test_basic_statistics(self) -> None:
        # Known array: [1, 2, 3, 4, 5] has median 3, p5 ≈ 1.2, p95 ≈ 4.8.
        samples = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        d = Distribution.from_samples(samples, UNITS, VER, pid())

        assert d.p50 == pytest.approx(np.percentile(samples, 50))
        assert d.p5 == pytest.approx(np.percentile(samples, 5))
        assert d.p95 == pytest.approx(np.percentile(samples, 95))
        assert d.mean == pytest.approx(3.0)
        assert d.std == pytest.approx(np.std(samples, ddof=0))
        assert d.units == UNITS
        assert d.methodology_version == VER

    def test_accepts_list_input(self) -> None:
        d = Distribution.from_samples([10.0, 20.0, 30.0], UNITS, VER, pid())
        assert d.p50 == pytest.approx(20.0)

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            Distribution.from_samples(np.array([]), UNITS, VER, pid())

    def test_rejects_2d_array(self) -> None:
        with pytest.raises(ValueError, match="1-D"):
            Distribution.from_samples(np.array([[1.0, 2.0], [3.0, 4.0]]), UNITS, VER, pid())

    def test_single_sample(self) -> None:
        d = Distribution.from_samples(np.array([7.0]), UNITS, VER, pid())
        # Degenerate — all percentiles equal the single value.
        assert d.p5 == d.p50 == d.p95 == pytest.approx(7.0)
        assert d.std == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Construction — from_normal
# ---------------------------------------------------------------------------


class TestFromNormal:
    def test_mean_and_std_converge(self) -> None:
        # N(100, 10²) with 100 000 samples: sample mean ≈ 100 ± 0.1
        d = Distribution.from_normal(100.0, 10.0, 100_000, UNITS, VER, pid(), seeded_rng(0))
        assert d.mean == pytest.approx(100.0, abs=0.5)
        assert d.std == pytest.approx(10.0, abs=0.5)

    def test_percentiles_converge(self) -> None:
        # N(0, 1): p5 ≈ -1.645, p95 ≈ +1.645
        d = Distribution.from_normal(0.0, 1.0, 100_000, UNITS, VER, pid(), seeded_rng(1))
        assert d.p5 == pytest.approx(-1.645, abs=0.05)
        assert d.p95 == pytest.approx(1.645, abs=0.05)

    def test_zero_std_is_constant(self) -> None:
        d = Distribution.from_normal(5.0, 0.0, 1_000, UNITS, VER, pid(), seeded_rng(2))
        assert d.p5 == pytest.approx(5.0)
        assert d.p95 == pytest.approx(5.0)

    def test_rejects_negative_std(self) -> None:
        with pytest.raises(ValueError, match="std must be"):
            Distribution.from_normal(1.0, -0.1, 100, UNITS, VER, pid())

    def test_rejects_zero_n_samples(self) -> None:
        with pytest.raises(ValueError, match="n_samples"):
            Distribution.from_normal(1.0, 1.0, 0, UNITS, VER, pid())


# ---------------------------------------------------------------------------
# Construction — from_triangular
# ---------------------------------------------------------------------------


class TestFromTriangular:
    def test_mode_is_most_likely(self) -> None:
        # Triangular(0, 10, 20): mean = (0+10+20)/3 = 10.0 exactly.
        d = Distribution.from_triangular(0.0, 10.0, 20.0, 100_000, UNITS, VER, pid(), seeded_rng(3))
        assert d.mean == pytest.approx(10.0, abs=0.1)

    def test_bounds_respected(self) -> None:
        d = Distribution.from_triangular(5.0, 7.0, 9.0, 10_000, UNITS, VER, pid(), seeded_rng(4))
        assert d.samples is not None
        assert float(d.samples.min()) >= 5.0
        assert float(d.samples.max()) <= 9.0

    def test_rejects_invalid_order(self) -> None:
        with pytest.raises(ValueError, match="low <= mode <= high"):
            Distribution.from_triangular(10.0, 5.0, 15.0, 100, UNITS, VER, pid())

    def test_degenerate_all_equal(self) -> None:
        # low == mode == high → constant
        d = Distribution.from_triangular(3.0, 3.0, 3.0, 1_000, UNITS, VER, pid(), seeded_rng(5))
        assert d.p50 == pytest.approx(3.0, abs=1e-10)


# ---------------------------------------------------------------------------
# Construction — from_lognormal
# ---------------------------------------------------------------------------


class TestFromLognormal:
    def test_median_is_exp_mu(self) -> None:
        # LogNormal(mu, sigma): median = exp(mu)
        mu, sigma = 2.0, 0.5
        d = Distribution.from_lognormal(mu, sigma, 100_000, UNITS, VER, pid(), seeded_rng(6))
        assert d.p50 == pytest.approx(math.exp(mu), rel=0.02)

    def test_all_samples_positive(self) -> None:
        d = Distribution.from_lognormal(0.0, 1.0, 10_000, UNITS, VER, pid(), seeded_rng(7))
        assert d.samples is not None
        assert float(d.samples.min()) > 0.0

    def test_rejects_nonpositive_sigma(self) -> None:
        with pytest.raises(ValueError, match="sigma must be > 0"):
            Distribution.from_lognormal(0.0, 0.0, 100, UNITS, VER, pid())
        with pytest.raises(ValueError, match="sigma must be > 0"):
            Distribution.from_lognormal(0.0, -1.0, 100, UNITS, VER, pid())


# ---------------------------------------------------------------------------
# Construction — from_uniform
# ---------------------------------------------------------------------------


class TestFromUniform:
    def test_mean_is_midpoint(self) -> None:
        # Uniform(2, 8): mean = 5.0 exactly.
        d = Distribution.from_uniform(2.0, 8.0, 100_000, UNITS, VER, pid(), seeded_rng(8))
        assert d.mean == pytest.approx(5.0, abs=0.05)

    def test_bounds_respected(self) -> None:
        d = Distribution.from_uniform(10.0, 20.0, 10_000, UNITS, VER, pid(), seeded_rng(9))
        assert d.samples is not None
        assert float(d.samples.min()) >= 10.0
        assert float(d.samples.max()) <= 20.0

    def test_degenerate_equal_bounds(self) -> None:
        d = Distribution.from_uniform(4.0, 4.0, 1_000, UNITS, VER, pid(), seeded_rng(10))
        assert d.p50 == pytest.approx(4.0)

    def test_rejects_inverted_bounds(self) -> None:
        with pytest.raises(ValueError, match="high must be >= low"):
            Distribution.from_uniform(10.0, 5.0, 100, UNITS, VER, pid())


# ---------------------------------------------------------------------------
# Construction — constant
# ---------------------------------------------------------------------------


class TestConstant:
    def test_all_percentiles_equal_value(self) -> None:
        d = Distribution.constant(42.5, UNITS, VER, pid())
        assert d.p5 == d.p50 == d.p95 == pytest.approx(42.5)
        assert d.mean == pytest.approx(42.5)
        assert d.std == pytest.approx(0.0)

    def test_has_one_sample(self) -> None:
        d = Distribution.constant(1.0, UNITS, VER, pid())
        assert d.samples is not None
        assert len(d.samples) == 1


# ---------------------------------------------------------------------------
# Percentile invariant
# ---------------------------------------------------------------------------


class TestPercentileInvariant:
    def test_valid_construction_passes(self) -> None:
        # Directly construct with valid percentiles — should not raise.
        d = Distribution(
            p5=1.0, p50=5.0, p95=9.0,
            mean=5.0, std=2.0,
            units=UNITS, methodology_version=VER,
            provenance_id=pid(),
        )
        assert d.p50 == 5.0

    def test_p5_greater_than_p50_raises(self) -> None:
        with pytest.raises(ValidationError, match="Percentile invariant"):
            Distribution(
                p5=6.0, p50=5.0, p95=9.0,
                mean=5.0, std=2.0,
                units=UNITS, methodology_version=VER,
                provenance_id=pid(),
            )

    def test_p50_greater_than_p95_raises(self) -> None:
        with pytest.raises(ValidationError, match="Percentile invariant"):
            Distribution(
                p5=1.0, p50=10.0, p95=9.0,
                mean=5.0, std=2.0,
                units=UNITS, methodology_version=VER,
                provenance_id=pid(),
            )

    def test_all_equal_is_valid(self) -> None:
        # p5 == p50 == p95 is valid (constant distribution).
        d = Distribution(
            p5=3.0, p50=3.0, p95=3.0,
            mean=3.0, std=0.0,
            units=UNITS, methodology_version=VER,
            provenance_id=pid(),
        )
        assert d.p5 == d.p95


# ---------------------------------------------------------------------------
# Arithmetic — Distribution + Distribution
# ---------------------------------------------------------------------------


class TestArithmeticDistributions:
    _rng = seeded_rng(20)
    _n = 1_000

    def _make(self, mean: float = 10.0, std: float = 2.0) -> Distribution:
        return Distribution.from_normal(mean, std, self._n, UNITS, VER, pid(), self._rng)

    def test_add_mean_is_sum_of_means(self) -> None:
        # For independent normals, E[A+B] = E[A] + E[B].
        a = Distribution.from_normal(10.0, 1.0, 10_000, UNITS, VER, pid(), seeded_rng(21))
        b = Distribution.from_normal(20.0, 1.0, 10_000, UNITS, VER, pid(), seeded_rng(22))
        c = a + b
        assert c.mean == pytest.approx(30.0, abs=0.2)

    def test_sub_mean_is_difference_of_means(self) -> None:
        a = Distribution.from_normal(30.0, 1.0, 10_000, UNITS, VER, pid(), seeded_rng(23))
        b = Distribution.from_normal(10.0, 1.0, 10_000, UNITS, VER, pid(), seeded_rng(24))
        c = a - b
        assert c.mean == pytest.approx(20.0, abs=0.2)

    def test_mul_mean_scales(self) -> None:
        # For A ~ N(5, 0) (constant) and B ~ N(3, 0), A*B has mean 15.
        a = Distribution.constant(5.0, UNITS, VER, pid())
        b = Distribution.constant(3.0, UNITS, VER, pid())
        # Extend to same length for element-wise multiply.
        a = Distribution.from_normal(5.0, 0.0, 1_000, UNITS, VER, pid(), seeded_rng(25))
        b = Distribution.from_normal(3.0, 0.0, 1_000, UNITS, VER, pid(), seeded_rng(26))
        c = a * b
        assert c.mean == pytest.approx(15.0, abs=0.1)

    def test_result_satisfies_percentile_invariant(self) -> None:
        a = Distribution.from_normal(5.0, 1.0, 1_000, UNITS, VER, pid(), seeded_rng(27))
        b = Distribution.from_normal(5.0, 1.0, 1_000, UNITS, VER, pid(), seeded_rng(28))
        for c in [a + b, a - b, a * b]:
            assert c.p5 <= c.p50 <= c.p95

    def test_methodology_version_mismatch_raises(self) -> None:
        a = Distribution.from_normal(1.0, 0.5, 100, UNITS, "1.0", pid(), seeded_rng(29))
        b = Distribution.from_normal(1.0, 0.5, 100, UNITS, "1.1", pid(), seeded_rng(30))
        with pytest.raises(ValueError, match="methodology versions"):
            _ = a + b

    def test_sample_count_mismatch_raises(self) -> None:
        a = Distribution.from_normal(1.0, 0.5, 100, UNITS, VER, pid(), seeded_rng(31))
        b = Distribution.from_normal(1.0, 0.5, 200, UNITS, VER, pid(), seeded_rng(32))
        with pytest.raises(ValueError, match="sample counts"):
            _ = a + b

    def test_combined_provenance_is_deterministic(self) -> None:
        pa, pb = pid(), pid()
        a = Distribution.from_samples(np.array([1.0, 2.0]), UNITS, VER, pa)
        b = Distribution.from_samples(np.array([3.0, 4.0]), UNITS, VER, pb)
        c1 = a + b
        c2 = a + b
        assert c1.provenance_id == c2.provenance_id

    def test_no_samples_raises_on_arithmetic(self) -> None:
        d = Distribution(
            p5=1.0, p50=5.0, p95=9.0,
            mean=5.0, std=2.0,
            units=UNITS, methodology_version=VER,
            provenance_id=pid(),
            samples=None,
        )
        with pytest.raises(ValueError, match="samples to be present"):
            _ = d + d


# ---------------------------------------------------------------------------
# Arithmetic — Distribution op scalar
# ---------------------------------------------------------------------------


class TestArithmeticScalar:
    _d = Distribution.from_normal(100.0, 10.0, 10_000, UNITS, VER, pid(), seeded_rng(40))

    def test_mul_scalar_scales_mean(self) -> None:
        result = self._d * 2.0
        assert result.mean == pytest.approx(self._d.mean * 2.0, rel=1e-6)

    def test_rmul_scalar_commutes(self) -> None:
        r1 = self._d * 3.0
        r2 = 3.0 * self._d
        assert r1.mean == pytest.approx(r2.mean)

    def test_add_scalar_shifts_mean(self) -> None:
        result = self._d + 50.0
        assert result.mean == pytest.approx(self._d.mean + 50.0, rel=1e-6)

    def test_radd_scalar(self) -> None:
        result = 50.0 + self._d
        assert result.mean == pytest.approx(self._d.mean + 50.0, rel=1e-6)

    def test_sub_scalar(self) -> None:
        result = self._d - 10.0
        assert result.mean == pytest.approx(self._d.mean - 10.0, rel=1e-6)

    def test_rsub_scalar(self) -> None:
        # 200 - D should have mean 200 - D.mean
        result = 200.0 - self._d
        assert result.mean == pytest.approx(200.0 - self._d.mean, rel=1e-6)

    def test_div_scalar(self) -> None:
        result = self._d / 4.0
        assert result.mean == pytest.approx(self._d.mean / 4.0, rel=1e-6)

    def test_scalar_op_preserves_methodology_version(self) -> None:
        result = self._d * 2.0
        assert result.methodology_version == VER

    def test_scalar_op_derives_new_provenance_id(self) -> None:
        # Scalar ops must NOT silently inherit the parent's provenance_id;
        # each transformation is a distinct audit step.
        result = self._d * 2.0
        assert result.provenance_id != self._d.provenance_id

    def test_scalar_op_derived_id_is_deterministic(self) -> None:
        r1 = self._d * 2.0
        r2 = self._d * 2.0
        assert r1.provenance_id == r2.provenance_id

    def test_apply_scalar_uses_explicit_provenance(self) -> None:
        factor_prov = uuid.uuid4()
        result = self._d.apply_scalar(3.15, op="mul", provenance_id=factor_prov)
        assert result.provenance_id == factor_prov
        assert result.mean == pytest.approx(self._d.mean * 3.15, rel=1e-6)

    def test_scalar_result_satisfies_invariant(self) -> None:
        for result in [self._d * 2.0, self._d + 5.0, self._d - 5.0, self._d / 2.0]:
            assert result.p5 <= result.p50 <= result.p95


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    def test_with_samples(self) -> None:
        original = Distribution.from_normal(50.0, 5.0, 500, UNITS, VER, pid(), seeded_rng(50))
        serialized = original.model_dump_json()
        restored = Distribution.model_validate_json(serialized)

        assert restored.p5 == pytest.approx(original.p5)
        assert restored.p50 == pytest.approx(original.p50)
        assert restored.p95 == pytest.approx(original.p95)
        assert restored.mean == pytest.approx(original.mean)
        assert restored.methodology_version == original.methodology_version
        assert restored.provenance_id == original.provenance_id
        assert restored.samples is not None
        np.testing.assert_allclose(restored.samples, original.samples)  # type: ignore[arg-type]

    def test_without_samples(self) -> None:
        original = Distribution.from_normal(50.0, 5.0, 500, UNITS, VER, pid(), seeded_rng(51))
        stripped = original.without_samples()
        serialized = stripped.model_dump_json()
        restored = Distribution.model_validate_json(serialized)

        assert restored.samples is None
        assert restored.p50 == pytest.approx(original.p50)

    def test_samples_serialized_as_list(self) -> None:
        d = Distribution.from_samples(np.array([1.0, 2.0, 3.0]), UNITS, VER, pid())
        payload = json.loads(d.model_dump_json())
        assert isinstance(payload["samples"], list)
        assert payload["samples"] == pytest.approx([1.0, 2.0, 3.0])

    def test_provenance_id_serialized_as_string(self) -> None:
        d = Distribution.from_samples(np.array([1.0]), UNITS, VER, pid())
        payload = json.loads(d.model_dump_json())
        assert isinstance(payload["provenance_id"], str)
        # Must be a valid UUID string.
        UUID(payload["provenance_id"])


# ---------------------------------------------------------------------------
# without_samples
# ---------------------------------------------------------------------------


class TestWithoutSamples:
    def test_strips_samples(self) -> None:
        d = Distribution.from_samples(np.array([1.0, 2.0, 3.0]), UNITS, VER, pid())
        stripped = d.without_samples()
        assert stripped.samples is None

    def test_preserves_percentiles(self) -> None:
        d = Distribution.from_normal(10.0, 2.0, 1_000, UNITS, VER, pid(), seeded_rng(60))
        stripped = d.without_samples()
        assert stripped.p5 == pytest.approx(d.p5)
        assert stripped.p50 == pytest.approx(d.p50)
        assert stripped.p95 == pytest.approx(d.p95)


# ---------------------------------------------------------------------------
# _combine_provenance_ids
# ---------------------------------------------------------------------------


class TestCombineProvenanceIds:
    def test_deterministic(self) -> None:
        a, b = pid(), pid()
        assert _combine_provenance_ids(a, b) == _combine_provenance_ids(a, b)

    def test_order_matters(self) -> None:
        a, b = pid(), pid()
        assert _combine_provenance_ids(a, b) != _combine_provenance_ids(b, a)

    def test_returns_uuid(self) -> None:
        a, b = pid(), pid()
        result = _combine_provenance_ids(a, b)
        assert isinstance(result, UUID)


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

# Build a strategy that generates (mean, std) pairs suitable for normal
# distributions used in arithmetic. Restrict ranges to avoid floating-point
# edge cases in percentile comparisons.
_finite_floats = st.floats(min_value=-1e4, max_value=1e4, allow_nan=False, allow_infinity=False)
_positive_std = st.floats(min_value=1.0, max_value=1e3, allow_nan=False, allow_infinity=False)


@given(
    mean_a=_finite_floats,
    std_a=_positive_std,
    mean_b=_finite_floats,
    std_b=_positive_std,
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=50, deadline=None)
def test_property_add_p50_bounded_by_operand_percentiles(
    mean_a: float,
    std_a: float,
    mean_b: float,
    std_b: float,
    seed: int,
) -> None:
    """(A + B).p50 lies within [A.p50 + B.p5, A.p50 + B.p95].

    For independent distributions the median of the sum tracks the sum of the
    medians. With large samples (50 000) the Monte Carlo error on percentiles
    is small enough that this property holds reliably. See methodology/v1.0.pdf
    §2.1 for the statistical justification.
    """
    n = 50_000
    prov = pid()
    a = Distribution.from_normal(mean_a, std_a, n, UNITS, VER, prov, np.random.default_rng(seed))
    b = Distribution.from_normal(mean_b, std_b, n, UNITS, VER, prov, np.random.default_rng(seed + 1))
    c = a + b

    # Lower bound: A.p50 + B.p5 ≤ (A+B).p50
    # Upper bound: (A+B).p50 ≤ A.p50 + B.p95
    lower = a.p50 + b.p5
    upper = a.p50 + b.p95

    # Small tolerance to absorb Monte Carlo sampling noise.
    tol = 0.01 * (abs(a.p50) + abs(b.p50) + std_a + std_b + 1.0)
    assert c.p50 >= lower - tol, f"p50={c.p50:.4f} < lower={lower:.4f} - tol={tol:.4f}"
    assert c.p50 <= upper + tol, f"p50={c.p50:.4f} > upper={upper:.4f} + tol={tol:.4f}"


@given(
    value=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    std=_positive_std,
    scalar=st.floats(min_value=0.01, max_value=1e3, allow_nan=False, allow_infinity=False),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=50, deadline=None)
def test_property_mul_scalar_scales_percentiles(
    value: float,
    std: float,
    scalar: float,
    seed: int,
) -> None:
    """Multiplying by a positive scalar scales all percentiles by that scalar."""
    d = Distribution.from_normal(value, std, 10_000, UNITS, VER, pid(), np.random.default_rng(seed))
    result = d * scalar

    assert result.p5 == pytest.approx(d.p5 * scalar, rel=1e-9)
    assert result.p50 == pytest.approx(d.p50 * scalar, rel=1e-9)
    assert result.p95 == pytest.approx(d.p95 * scalar, rel=1e-9)
    # Invariant must hold in the result.
    assert result.p5 <= result.p50 <= result.p95


@given(
    mean=_finite_floats,
    std=_positive_std,
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=100, deadline=None)
def test_property_invariant_always_holds_after_construction(
    mean: float,
    std: float,
    seed: int,
) -> None:
    """from_normal always produces p5 <= p50 <= p95, regardless of parameters."""
    d = Distribution.from_normal(mean, std, 1_000, UNITS, VER, pid(), np.random.default_rng(seed))
    assert d.p5 <= d.p50 <= d.p95
