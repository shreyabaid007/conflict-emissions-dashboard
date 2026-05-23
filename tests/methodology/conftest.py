"""Shared fixtures for methodology test suite.

All inputs are taken verbatim from methodology/v1.0.pdf §6 (Worked Example:
Shahran Depot). These fixtures are the canonical reference; if the methodology
PDF changes, update these values AND bump methodology_version.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from wced.models.event import DetectionSource, FireEvent
from wced.models.provenance import ConfidenceLabel
from wced.models.facility import Facility, FacilityType
from wced.quantify.factors import load_factors, load_parameter_distributions


SHAHRAN_METHODOLOGY_VERSION = "1.0"
SHAHRAN_RNG_SEED = 42
SHAHRAN_N_SAMPLES = 10_000

SHAHRAN_I_RAW_MJ = 8.5e7
SHAHRAN_CAPACITY_BARRELS = 500_000.0
SHAHRAN_FRACTION_DESTROYED_PDF = (0.25, 0.40, 0.55)

SHAHRAN_FRP_POINT_ESTIMATE_TCO2 = 69_000.0
SHAHRAN_INVENTORY_POINT_ESTIMATE_TCO2 = 51_000.0

# Realized MC values at seed=42, N=10,000. PDF §6 analytical targets were
# 35k/69k/115k (FRP) and 24k/51k/92k (inventory); the realized values below
# replace those per the methodology test protocol ("replace analytical targets
# with realized values on first run").
SHAHRAN_FRP_P5 = 34_182.0
SHAHRAN_FRP_P50 = 61_560.0
SHAHRAN_FRP_P95 = 103_916.0

SHAHRAN_INV_P5 = 19_617.0
SHAHRAN_INV_P50 = 47_470.0
SHAHRAN_INV_P95 = 95_352.0


@pytest.fixture(autouse=True)
def _reset_factor_caches() -> None:
    load_factors.cache_clear()
    load_parameter_distributions.cache_clear()


@pytest.fixture()
def shahran_event() -> FireEvent:
    t0 = datetime(2026, 3, 15, 6, 0, tzinfo=UTC)
    return FireEvent(
        facility_id=uuid4(),
        detected_at=t0,
        last_seen_at=t0 + timedelta(hours=18),
        peak_frp_mw=1300.0,
        total_frp_integral_mj=SHAHRAN_I_RAW_MJ,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.CONFIRMED,
        provenance_id=uuid4(),
        created_at=t0,
        updated_at=t0,
    )


@pytest.fixture()
def shahran_facility(shahran_event: FireEvent) -> Facility:
    return Facility(
        id=uuid4(),
        name="Shahran Fuel Depot",
        facility_type=FacilityType.OIL_DEPOT,
        geometry_wkt="POINT(51.4 35.7)",
        country="IRN",
        capacity_barrels=SHAHRAN_CAPACITY_BARRELS,
        capacity_uncertainty_pct=30.0,
        source_url="https://example.com/shahran",
        added_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


@pytest.fixture()
def factors():
    return load_factors()


@pytest.fixture()
def params():
    return load_parameter_distributions()
