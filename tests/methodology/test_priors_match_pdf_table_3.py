"""Verify every prior in parameter_distributions.yaml matches methodology PDF Table 3.

Table 2 in the PDF (§5, titled "Monte Carlo parameter priors") includes both
emission factors and parameter priors. The parameter priors are stored in
parameter_distributions.yaml. This test verifies that the YAML matches the PDF.
"""
from __future__ import annotations

import pytest

from wced.quantify.factors import load_parameter_distributions


EXPECTED_TABLE_3 = {
    "burn_duty_cycle": {
        "value": 0.7,
        "distribution": "triangular",
        "low": 0.4,
        "mode": 0.7,
        "high": 0.95,
        "units": "dimensionless",
        "methodology_section": "4.1.2",
    },
    "facility_inventory_at_strike": {
        "value": 0.6,
        "distribution": "uniform",
        "low": 0.3,
        "high": 0.9,
        "units": "dimensionless",
        "methodology_section": "4.2.1",
    },
    "frp_extrapolation_factor": {
        "value": 1.0,
        "distribution": "normal",
        "sigma": 0.15,
        "units": "dimensionless",
        "methodology_section": "4.3.1",
    },
}


class TestPriorsMatchTable3:

    @pytest.fixture(autouse=True)
    def _load(self):
        self.registry = load_parameter_distributions()

    @pytest.mark.parametrize("key", list(EXPECTED_TABLE_3.keys()))
    def test_prior_value(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_3[key]
        assert factor.value == pytest.approx(expected["value"], abs=1e-6), (
            f"{key}: value {factor.value} != expected {expected['value']}"
        )

    @pytest.mark.parametrize("key", list(EXPECTED_TABLE_3.keys()))
    def test_prior_distribution_type(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_3[key]
        assert factor.distribution == expected["distribution"], (
            f"{key}: distribution {factor.distribution} != expected {expected['distribution']}"
        )

    @pytest.mark.parametrize("key", list(EXPECTED_TABLE_3.keys()))
    def test_prior_units(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_3[key]
        assert factor.units == expected["units"]

    @pytest.mark.parametrize("key", list(EXPECTED_TABLE_3.keys()))
    def test_prior_methodology_section(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_3[key]
        assert factor.methodology_section == expected["methodology_section"]

    @pytest.mark.parametrize(
        "key",
        [k for k, v in EXPECTED_TABLE_3.items() if v["distribution"] == "triangular"],
    )
    def test_triangular_params(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_3[key]
        assert factor.low == pytest.approx(expected["low"], abs=1e-6)
        assert factor.mode == pytest.approx(expected["mode"], abs=1e-6)
        assert factor.high == pytest.approx(expected["high"], abs=1e-6)

    @pytest.mark.parametrize(
        "key",
        [k for k, v in EXPECTED_TABLE_3.items() if v["distribution"] == "uniform"],
    )
    def test_uniform_params(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_3[key]
        assert factor.low == pytest.approx(expected["low"], abs=1e-6)
        assert factor.high == pytest.approx(expected["high"], abs=1e-6)

    @pytest.mark.parametrize(
        "key",
        [k for k, v in EXPECTED_TABLE_3.items() if v["distribution"] == "normal"],
    )
    def test_normal_params(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_3[key]
        assert factor.sigma == pytest.approx(expected["sigma"], abs=1e-6)

    def test_all_expected_keys_present(self) -> None:
        for key in EXPECTED_TABLE_3:
            assert key in self.registry, f"Missing prior {key!r} from parameter_distributions.yaml"
