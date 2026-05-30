"""Tests for wced.validate.tropomi — TROPOMI plume detection and back-calculation.

Uses synthetic plume data to test bias correction, enhancement detection,
back-calculation, and discrepancy computation without live API calls.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import numpy as np
import pytest
import xarray as xr

from wced.models.event import DetectionSource, EventStatus, FireEvent
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord, Source, SourceType
from wced.validate.tropomi import (
    BackCalculation,
    DiscrepancyResult,
    PlumeDetection,
    _apply_bias_correction,
    _detect_enhancement,
    _emission_rate_to_co2,
    _NO2_CORRECTION_FACTOR,
    _variable_name,
    back_calculate_emissions,
    compute_discrepancy,
    detect_no2_plume_at,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    *,
    frp_integral: float = 5000.0,
    status: EventStatus = EventStatus.PUBLISHED,
) -> FireEvent:
    t0 = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    return FireEvent(
        facility_id=uuid4(),
        detected_at=t0,
        last_seen_at=t0 + timedelta(hours=12),
        peak_frp_mw=200.0,
        total_frp_integral_mj=frp_integral,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.CONFIRMED,
        status=status,
        provenance_id=uuid4(),
        created_at=t0,
        updated_at=t0,
    )


def _source() -> Source:
    return Source(
        source_type=SourceType.SATELLITE,
        identifier="test-granule-001",
        retrieved_at=datetime.now(tz=UTC),
        retrieved_by="tests.unit.validate.test_tropomi",
        content_hash="abc123",
        metadata={"bias_warning": "TROPOMI NO2 v2.x bias"},
    )


def _synthetic_no2_dataset(
    n_scanlines: int = 20,
    n_pixels: int = 30,
    background: float = 5e15,
    plume_enhancement: float = 15e15,
    plume_fraction: float = 0.1,
    seed: int = 42,
) -> xr.Dataset:
    """Build a synthetic TROPOMI NO2 dataset with an embedded plume.

    Parameters
    ----------
    n_scanlines, n_pixels : int
        Grid dimensions.
    background : float
        Background NO2 column density (mol/m²).
    plume_enhancement : float
        Plume pixel column density (mol/m²).
    plume_fraction : float
        Fraction of pixels that are plume-enhanced.
    seed : int
        RNG seed for reproducibility.
    """
    rng = np.random.default_rng(seed)
    values = rng.normal(background, background * 0.1, (n_scanlines, n_pixels))

    n_plume = int(n_scanlines * n_pixels * plume_fraction)
    plume_indices = rng.choice(
        n_scanlines * n_pixels, size=n_plume, replace=False,
    )
    flat = values.ravel()
    flat[plume_indices] = rng.normal(
        plume_enhancement, plume_enhancement * 0.1, n_plume,
    )
    values = flat.reshape(n_scanlines, n_pixels)

    lats = np.linspace(32.0, 33.0, n_scanlines)[:, np.newaxis] * np.ones(n_pixels)
    lons = np.linspace(51.0, 52.0, n_pixels)[np.newaxis, :] * np.ones((n_scanlines, 1))
    qa = np.ones((n_scanlines, n_pixels)) * 0.85

    return xr.Dataset(
        {
            "nitrogendioxide_tropospheric_column": (
                ("scanline", "ground_pixel"), values,
            ),
            "qa_value": (("scanline", "ground_pixel"), qa),
            "latitude": (("scanline", "ground_pixel"), lats),
            "longitude": (("scanline", "ground_pixel"), lons),
        }
    )


def _synthetic_co_dataset(
    n_scanlines: int = 20,
    n_pixels: int = 30,
    background: float = 0.02,
    seed: int = 42,
) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    values = rng.normal(background, background * 0.05, (n_scanlines, n_pixels))
    lats = np.linspace(32.0, 33.0, n_scanlines)[:, np.newaxis] * np.ones(n_pixels)
    lons = np.linspace(51.0, 52.0, n_pixels)[np.newaxis, :] * np.ones((n_scanlines, 1))
    qa = np.ones((n_scanlines, n_pixels)) * 0.85

    return xr.Dataset(
        {
            "carbonmonoxide_total_column": (
                ("scanline", "ground_pixel"), values,
            ),
            "qa_value": (("scanline", "ground_pixel"), qa),
            "latitude": (("scanline", "ground_pixel"), lats),
            "longitude": (("scanline", "ground_pixel"), lons),
        }
    )


# ---------------------------------------------------------------------------
# _variable_name
# ---------------------------------------------------------------------------


class TestVariableName:
    def test_no2(self) -> None:
        assert _variable_name("NO2") == "nitrogendioxide_tropospheric_column"

    def test_co(self) -> None:
        assert _variable_name("CO") == "carbonmonoxide_total_column"

    def test_case_insensitive(self) -> None:
        assert _variable_name("no2") == "nitrogendioxide_tropospheric_column"

    def test_unsupported_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported product"):
            _variable_name("SO2")


# ---------------------------------------------------------------------------
# _apply_bias_correction
# ---------------------------------------------------------------------------


class TestBiasCorrection:
    def test_no2_correction_applied(self) -> None:
        ds = _synthetic_no2_dataset(background=1e16, plume_enhancement=1e16)
        var = "nitrogendioxide_tropospheric_column"
        raw_mean = float(ds[var].mean())

        ds_corrected, record = _apply_bias_correction(ds, var, "NO2", _source())

        corrected_mean = float(ds_corrected[var].mean())
        expected = raw_mean * _NO2_CORRECTION_FACTOR
        assert abs(corrected_mean - expected) / expected < 1e-6

    def test_no2_correction_factor_is_positive(self) -> None:
        assert _NO2_CORRECTION_FACTOR > 1.0, "Correction should increase values"
        assert abs(_NO2_CORRECTION_FACTOR - 1.0 / 0.77) < 0.01

    def test_no2_provenance_recorded(self) -> None:
        ds = _synthetic_no2_dataset()
        var = "nitrogendioxide_tropospheric_column"
        source = _source()

        _, record = _apply_bias_correction(ds, var, "NO2", source)

        assert record.produced_by == "wced.validate.tropomi"
        assert record.method == "tropomi_no2_bias_correction_v1.0"
        assert source.id in record.inputs
        assert record.parameters["bias_fraction"] == -0.23
        assert record.parameters["correction_factor"] == _NO2_CORRECTION_FACTOR
        assert "van Geffen" in record.parameters["reference"]

    def test_no2_provenance_deterministic(self) -> None:
        ds = _synthetic_no2_dataset()
        var = "nitrogendioxide_tropospheric_column"
        source = _source()

        _, r1 = _apply_bias_correction(ds, var, "NO2", source)
        _, r2 = _apply_bias_correction(ds, var, "NO2", source)

        assert r1.id == r2.id

    def test_co_no_correction(self) -> None:
        ds = _synthetic_co_dataset()
        var = "carbonmonoxide_total_column"
        raw_mean = float(ds[var].mean())

        ds_corrected, record = _apply_bias_correction(ds, var, "CO", _source())

        assert ds_corrected is ds, "CO should return the same dataset object"
        assert record.method == "tropomi_passthrough_v1.0"
        assert record.parameters["correction_applied"] is False

    def test_no2_does_not_modify_original(self) -> None:
        ds = _synthetic_no2_dataset()
        var = "nitrogendioxide_tropospheric_column"
        original_values = ds[var].values.copy()

        _apply_bias_correction(ds, var, "NO2", _source())

        np.testing.assert_array_equal(ds[var].values, original_values)


# ---------------------------------------------------------------------------
# _detect_enhancement
# ---------------------------------------------------------------------------


class TestDetectEnhancement:
    def test_plume_detected_in_synthetic_data(self) -> None:
        ds = _synthetic_no2_dataset(
            background=5e15, plume_enhancement=20e15, plume_fraction=0.1,
        )
        var = "nitrogendioxide_tropospheric_column"

        bg_mean, bg_std, enh_mean, enh_count, total = _detect_enhancement(ds, var)

        assert total == 20 * 30
        assert enh_count > 0
        assert enh_mean > bg_mean
        assert bg_std > 0

    def test_no_plume_in_uniform_data(self) -> None:
        rng = np.random.default_rng(99)
        values = rng.normal(5e15, 5e13, (20, 30))
        ds = xr.Dataset({
            "nitrogendioxide_tropospheric_column": (
                ("scanline", "ground_pixel"), values,
            ),
        })

        bg_mean, bg_std, enh_mean, enh_count, total = _detect_enhancement(
            ds, "nitrogendioxide_tropospheric_column",
        )

        assert total == 600
        # With tight normal (CV=1%), enhancement threshold is bg_25pct_mean + 3σ.
        # Some pixels may still exceed this; the key assertion is that the
        # enhancement mean is NOT dramatically above background.
        if enh_count > 0:
            assert enh_mean < bg_mean * 1.5, "Enhancement should not be far above background"

    def test_empty_dataset(self) -> None:
        values = np.full((5, 5), np.nan)
        ds = xr.Dataset({
            "nitrogendioxide_tropospheric_column": (
                ("scanline", "ground_pixel"), values,
            ),
        })

        bg_mean, bg_std, enh_mean, enh_count, total = _detect_enhancement(
            ds, "nitrogendioxide_tropospheric_column",
        )

        assert total == 0
        assert enh_count == 0


# ---------------------------------------------------------------------------
# compute_discrepancy
# ---------------------------------------------------------------------------


class TestComputeDiscrepancy:
    def test_ratio_1_not_flagged(self) -> None:
        ev = _event()
        bc = BackCalculation(
            event_id=ev.id,
            emission_rate_kg_per_s=1.0,
            emission_rate_uncertainty_factor=2.0,
            implied_co2_tonnes=10_000.0,
            hysplit_config={},
            provenance_record=_make_prov_record(),
        )

        result = compute_discrepancy(ev, bc, 10_000.0)

        assert abs(result.ratio - 1.0) < 1e-6
        assert result.flagged is False

    def test_ratio_3_flagged(self) -> None:
        ev = _event()
        bc = BackCalculation(
            event_id=ev.id,
            emission_rate_kg_per_s=1.0,
            emission_rate_uncertainty_factor=2.0,
            implied_co2_tonnes=30_000.0,
            hysplit_config={},
            provenance_record=_make_prov_record(),
        )

        result = compute_discrepancy(ev, bc, 10_000.0)

        assert result.ratio == 3.0
        assert result.flagged is True

    def test_ratio_below_half_flagged(self) -> None:
        ev = _event()
        bc = BackCalculation(
            event_id=ev.id,
            emission_rate_kg_per_s=1.0,
            emission_rate_uncertainty_factor=2.0,
            implied_co2_tonnes=4_000.0,
            hysplit_config={},
            provenance_record=_make_prov_record(),
        )

        result = compute_discrepancy(ev, bc, 10_000.0)

        assert result.ratio == 0.4
        assert result.flagged is True

    def test_ratio_exactly_2_not_flagged(self) -> None:
        """log2(2.0) = 1.0, which is NOT > 1, so ratio=2.0 is not flagged."""
        ev = _event()
        bc = BackCalculation(
            event_id=ev.id,
            emission_rate_kg_per_s=1.0,
            emission_rate_uncertainty_factor=2.0,
            implied_co2_tonnes=20_000.0,
            hysplit_config={},
            provenance_record=_make_prov_record(),
        )

        result = compute_discrepancy(ev, bc, 10_000.0)

        assert result.ratio == 2.0
        assert result.flagged is False

    def test_zero_bottom_up_raises(self) -> None:
        ev = _event()
        bc = BackCalculation(
            event_id=ev.id,
            emission_rate_kg_per_s=1.0,
            emission_rate_uncertainty_factor=2.0,
            implied_co2_tonnes=10_000.0,
            hysplit_config={},
            provenance_record=_make_prov_record(),
        )

        with pytest.raises(ValueError, match="positive"):
            compute_discrepancy(ev, bc, 0.0)


# ---------------------------------------------------------------------------
# _emission_rate_to_co2
# ---------------------------------------------------------------------------


class TestEmissionRateToCO2:
    def test_no2_conversion_positive(self) -> None:
        result = _emission_rate_to_co2(1.0, "NO2", uuid4())
        assert result > 0

    def test_co_conversion_positive(self) -> None:
        result = _emission_rate_to_co2(1.0, "CO", uuid4())
        assert result > 0

    def test_no2_produces_more_co2_per_kg_than_co(self) -> None:
        """NO2 has a lower molar ratio to CO2, so 1 kg NO2 implies more CO2."""
        no2_co2 = _emission_rate_to_co2(1.0, "NO2", uuid4())
        co_co2 = _emission_rate_to_co2(1.0, "CO", uuid4())
        assert no2_co2 > co_co2

    def test_zero_rate_gives_zero(self) -> None:
        assert _emission_rate_to_co2(0.0, "NO2", uuid4()) == 0.0

    def test_unsupported_product_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported product"):
            _emission_rate_to_co2(1.0, "SO2", uuid4())

    def test_co2_order_of_magnitude_no2(self) -> None:
        """1 kg/s of NO2 over 24h should give a very large CO2 number."""
        result = _emission_rate_to_co2(1.0, "NO2", uuid4())
        # 1 kg/s NO2 → (1/0.046)/0.004 × 0.044 × 86400 / 1000
        # ≈ 21.74/0.004 = 5434.8 mol CO2/s × 0.044 = 239 kg CO2/s × 86.4 = 20,650 t
        assert result > 1_000, f"Expected >1000 tCO2, got {result}"


# ---------------------------------------------------------------------------
# PlumeDetection construction (synthetic)
# ---------------------------------------------------------------------------


class TestPlumeDetection:
    def test_synthetic_plume_attributes(self) -> None:
        ev = _event()
        source = _source()
        ds = _synthetic_no2_dataset()
        var = "nitrogendioxide_tropospheric_column"
        ds_corr, record = _apply_bias_correction(ds, var, "NO2", source)
        bg_mean, bg_std, enh_mean, enh_count, total = _detect_enhancement(ds_corr, var)

        plume = PlumeDetection(
            event_id=ev.id,
            product="NO2",
            corrected_dataset=ds_corr,
            background_mean=bg_mean,
            background_std=bg_std,
            enhancement_mean=enh_mean,
            enhancement_pixels=enh_count,
            total_pixels=total,
            plume_detected=enh_count > 0,
            source=source,
            bias_correction_record=record,
        )

        assert plume.event_id == ev.id
        assert plume.product == "NO2"
        assert plume.total_pixels == 600


# ---------------------------------------------------------------------------
# back_calculate_emissions (with synthetic plume)
# ---------------------------------------------------------------------------


class TestBackCalculateEmissions:
    def _make_plume(self, *, plume_detected: bool = True) -> PlumeDetection:
        ev = _event()
        source = _source()
        ds = _synthetic_no2_dataset(
            background=5e15, plume_enhancement=20e15, plume_fraction=0.1,
        )
        var = "nitrogendioxide_tropospheric_column"
        ds_corr, record = _apply_bias_correction(ds, var, "NO2", source)
        bg_mean, bg_std, enh_mean, enh_count, total = _detect_enhancement(ds_corr, var)

        return PlumeDetection(
            event_id=ev.id,
            product="NO2",
            corrected_dataset=ds_corr,
            background_mean=bg_mean if plume_detected else 0.0,
            background_std=bg_std if plume_detected else 0.0,
            enhancement_mean=enh_mean if plume_detected else 0.0,
            enhancement_pixels=enh_count if plume_detected else 0,
            total_pixels=total,
            plume_detected=plume_detected,
            source=source,
            bias_correction_record=record,
        )

    def test_positive_emission_rate(self) -> None:
        plume = self._make_plume()
        result = back_calculate_emissions(plume, wind_speed_m_s=5.0, wind_direction_deg=270.0)

        assert result.emission_rate_kg_per_s > 0
        assert result.implied_co2_tonnes > 0

    def test_uncertainty_factor_is_2(self) -> None:
        plume = self._make_plume()
        result = back_calculate_emissions(plume, wind_speed_m_s=5.0, wind_direction_deg=270.0)

        assert result.emission_rate_uncertainty_factor == 2.0

    def test_provenance_recorded(self) -> None:
        plume = self._make_plume()
        result = back_calculate_emissions(plume, wind_speed_m_s=5.0, wind_direction_deg=270.0)

        assert result.provenance_record.produced_by == "wced.validate.tropomi"
        assert result.provenance_record.method == "hysplit_inverse_v1.0"
        assert result.provenance_record.confidence_label == ConfidenceLabel.SUSPECTED
        assert plume.source.id in result.provenance_record.inputs
        assert plume.bias_correction_record.id in result.provenance_record.inputs

    def test_zero_enhancement_gives_zero_rate(self) -> None:
        plume = self._make_plume(plume_detected=False)
        result = back_calculate_emissions(plume, wind_speed_m_s=5.0, wind_direction_deg=270.0)

        assert result.emission_rate_kg_per_s == 0.0

    def test_hysplit_config_recorded(self) -> None:
        plume = self._make_plume()
        result = back_calculate_emissions(plume, wind_speed_m_s=7.5, wind_direction_deg=180.0)

        assert result.hysplit_config["wind_speed_m_s"] == 7.5
        assert result.hysplit_config["wind_direction_deg"] == 180.0

    def test_provenance_deterministic(self) -> None:
        plume = self._make_plume()
        r1 = back_calculate_emissions(plume, wind_speed_m_s=5.0, wind_direction_deg=270.0)
        r2 = back_calculate_emissions(plume, wind_speed_m_s=5.0, wind_direction_deg=270.0)

        assert r1.provenance_record.id == r2.provenance_record.id


# ---------------------------------------------------------------------------
# Helpers used in tests
# ---------------------------------------------------------------------------


def _make_prov_record() -> ProvenanceRecord:
    return ProvenanceRecord(
        produced_by="test",
        method="test_method",
        produced_at=datetime.now(tz=UTC),
        confidence_label=ConfidenceLabel.SUSPECTED,
    )
