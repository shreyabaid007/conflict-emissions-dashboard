"""Verify every factor in emission_factors.yaml matches methodology PDF Table 2.

Table 2 (methodology/v1.0.pdf §5) lists the central values and distribution
parameters for all emission factors. This test loads the YAML and asserts
each entry matches the PDF exactly.
"""
from __future__ import annotations

import pytest

from wced.quantify.factors import load_factors


EXPECTED_TABLE_2 = {
    "crude_oil_combustion": {
        "value": 0.425,
        "distribution": "triangular",
        "low": 0.405,
        "mode": 0.425,
        "high": 0.445,
        "units": "tCO2_per_barrel",
        "methodology_section": "3.2.1",
    },
    "refined_product_combustion": {
        "value": 0.430,
        "distribution": "triangular",
        "low": 0.410,
        "mode": 0.430,
        "high": 0.455,
        "units": "tCO2_per_barrel",
        "methodology_section": "3.2.2",
    },
    "frp_to_combustion_rate": {
        "value": 0.368,
        "distribution": "normal",
        "sigma": 0.05,
        "units": "kg_per_MJ",
        "methodology_section": "3.3.2",
    },
    "carbon_recovery_as_co2": {
        "value": 0.96,
        "distribution": "triangular",
        "low": 0.92,
        "mode": 0.96,
        "high": 0.98,
        "units": "dimensionless",
        "methodology_section": "3.3.3",
    },
}


class TestFactorsMatchTable2:

    @pytest.fixture(autouse=True)
    def _load(self):
        self.registry = load_factors()

    @pytest.mark.parametrize("key", list(EXPECTED_TABLE_2.keys()))
    def test_factor_value(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_2[key]
        assert factor.value == pytest.approx(expected["value"], abs=1e-6), (
            f"{key}: value {factor.value} != expected {expected['value']}"
        )

    @pytest.mark.parametrize("key", list(EXPECTED_TABLE_2.keys()))
    def test_factor_distribution_type(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_2[key]
        assert factor.distribution == expected["distribution"], (
            f"{key}: distribution {factor.distribution} != expected {expected['distribution']}"
        )

    @pytest.mark.parametrize("key", list(EXPECTED_TABLE_2.keys()))
    def test_factor_units(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_2[key]
        assert factor.units == expected["units"], (
            f"{key}: units {factor.units} != expected {expected['units']}"
        )

    @pytest.mark.parametrize("key", list(EXPECTED_TABLE_2.keys()))
    def test_factor_methodology_section(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_2[key]
        assert factor.methodology_section == expected["methodology_section"]

    @pytest.mark.parametrize(
        "key",
        [k for k, v in EXPECTED_TABLE_2.items() if v["distribution"] == "triangular"],
    )
    def test_triangular_params(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_2[key]
        assert factor.low == pytest.approx(expected["low"], abs=1e-6)
        assert factor.mode == pytest.approx(expected["mode"], abs=1e-6)
        assert factor.high == pytest.approx(expected["high"], abs=1e-6)

    @pytest.mark.parametrize(
        "key",
        [k for k, v in EXPECTED_TABLE_2.items() if v["distribution"] == "normal"],
    )
    def test_normal_params(self, key: str) -> None:
        factor = self.registry[key]
        expected = EXPECTED_TABLE_2[key]
        assert factor.sigma == pytest.approx(expected["sigma"], abs=1e-6)

    def test_all_expected_keys_present(self) -> None:
        for key in EXPECTED_TABLE_2:
            assert key in self.registry, f"Missing factor {key!r} from emission_factors.yaml"
