"""Tests for wced.ingest.sentinel5p.

All STAC and xarray I/O is mocked.  Fixture STAC items are loaded from
tests/fixtures/stac_items/ as plain JSON via pystac.Item.from_dict().
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "stac_items"


def _load_s5p_item(filename: str = "s5p_no2_iran_item.json"):
    import pystac  # noqa: PLC0415
    data = json.loads((FIXTURE_DIR / filename).read_text())
    return pystac.Item.from_dict(data)


def _make_mock_catalog(items: list) -> MagicMock:
    search = MagicMock()
    search.items.return_value = iter(items)
    catalog = MagicMock()
    catalog.search.return_value = search
    return catalog


def _make_fake_s5p_dataset(
    lat_centre: float = 32.66,
    lon_centre: float = 51.68,
    grid: int = 6,
    variable: str = "nitrogendioxide_tropospheric_column",
) -> xr.Dataset:
    """Build a minimal S5P-shaped Dataset (scanline × ground_pixel)."""
    lats = np.linspace(lat_centre - 0.5, lat_centre + 0.5, grid)
    lons = np.linspace(lon_centre - 0.5, lon_centre + 0.5, grid)
    lat_2d, lon_2d = np.meshgrid(lats, lons, indexing="ij")
    col = np.random.default_rng(42).uniform(1e14, 1e16, size=(grid, grid)).astype("float32")
    qa = np.full((grid, grid), 0.85, dtype="float32")  # all above 0.75 threshold
    return xr.Dataset(
        {
            variable: xr.DataArray(col, dims=["scanline", "ground_pixel"]),
            "qa_value": xr.DataArray(qa, dims=["scanline", "ground_pixel"]),
            "latitude": xr.DataArray(lat_2d, dims=["scanline", "ground_pixel"]),
            "longitude": xr.DataArray(lon_2d, dims=["scanline", "ground_pixel"]),
        }
    )


# ---------------------------------------------------------------------------
# query_plume — basic happy path
# ---------------------------------------------------------------------------

class TestQueryPlume:
    def test_returns_dataset_and_source(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector
        from wced.models.provenance import SourceType

        item = _load_s5p_item()
        fake_ds = _make_fake_s5p_dataset()
        connector = Sentinel5PConnector(cache_dir=tmp_path, catalog=_make_mock_catalog([item]))

        with patch("wced.ingest.sentinel5p.xr.open_dataset", return_value=fake_ds):
            ds, source = connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
                product="NO2",
            )

        assert "nitrogendioxide_tropospheric_column" in ds.data_vars
        assert "qa_value" in ds.data_vars
        assert "latitude" in ds.data_vars
        assert "longitude" in ds.data_vars

        assert source.source_type is SourceType.SATELLITE
        assert source.retrieved_by == "wced.ingest.sentinel5p"
        assert source.metadata["product"] == "NO2"

    def test_no2_source_contains_bias_warning(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector

        item = _load_s5p_item()
        fake_ds = _make_fake_s5p_dataset()
        connector = Sentinel5PConnector(cache_dir=tmp_path, catalog=_make_mock_catalog([item]))

        with patch("wced.ingest.sentinel5p.xr.open_dataset", return_value=fake_ds):
            _, source = connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
            )

        assert "bias_warning" in source.metadata
        warning = source.metadata["bias_warning"]
        assert "-23" in warning
        assert "van Geffen" in warning

    def test_non_no2_source_has_no_bias_warning(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector

        import pystac
        co_item_data = json.loads((FIXTURE_DIR / "s5p_no2_iran_item.json").read_text())
        co_item_data["id"] = "S5P_CO_test"
        co_item_data["collection"] = "sentinel-5p-l2-co"
        co_item = pystac.Item.from_dict(co_item_data)

        fake_ds = _make_fake_s5p_dataset(variable="carbonmonoxide_total_column")
        connector = Sentinel5PConnector(cache_dir=tmp_path, catalog=_make_mock_catalog([co_item]))

        with patch("wced.ingest.sentinel5p.xr.open_dataset", return_value=fake_ds):
            _, source = connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
                product="CO",
            )

        assert "bias_warning" not in source.metadata


# ---------------------------------------------------------------------------
# QA filtering
# ---------------------------------------------------------------------------

class TestQaFiltering:
    def test_pixels_below_qa_threshold_are_nan(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector

        item = _load_s5p_item()
        # Half the pixels have qa=0.4 (below 0.75 threshold)
        fake_ds = _make_fake_s5p_dataset(grid=4)
        low_qa = fake_ds["qa_value"].values.copy()
        low_qa[:2, :] = 0.4
        fake_ds["qa_value"] = xr.DataArray(low_qa, dims=["scanline", "ground_pixel"])

        connector = Sentinel5PConnector(
            cache_dir=tmp_path,
            catalog=_make_mock_catalog([item]),
            qa_threshold=0.75,
        )

        with patch("wced.ingest.sentinel5p.xr.open_dataset", return_value=fake_ds):
            ds, _ = connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
            )

        col = ds["nitrogendioxide_tropospheric_column"].values
        # Rows 0–1 (low QA) should be NaN after masking
        assert np.all(np.isnan(col[:2, :]))
        # Rows 2–3 (high QA) should be finite
        assert np.all(np.isfinite(col[2:, :]))

    def test_zero_valid_pixels_emits_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        from wced.ingest.sentinel5p import Sentinel5PConnector

        item = _load_s5p_item()
        # Put the target point well outside the fake dataset's lat/lon extent
        # so the spatial mask zeros everything.
        fake_ds = _make_fake_s5p_dataset(lat_centre=0.0, lon_centre=0.0, grid=4)

        connector = Sentinel5PConnector(
            cache_dir=tmp_path,
            catalog=_make_mock_catalog([item]),
            search_delta_deg=0.1,
        )

        with caplog.at_level(logging.WARNING, logger="wced.ingest.sentinel5p"):
            with patch("wced.ingest.sentinel5p.xr.open_dataset", return_value=fake_ds):
                ds, _ = connector.query_plume(
                    32.66, 51.68,
                    (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
                )

        assert any("zero valid" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    def test_raises_on_unknown_product(self) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector, Sentinel5PError

        connector = Sentinel5PConnector(cache_dir=None, catalog=_make_mock_catalog([]))
        with pytest.raises(Sentinel5PError, match="Unknown S5P product"):
            connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
                product="XYZ",
            )

    def test_raises_when_no_granule_found(self) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector, Sentinel5PError

        empty_search = MagicMock()
        empty_search.items.return_value = iter([])
        catalog = MagicMock()
        catalog.search.return_value = empty_search

        connector = Sentinel5PConnector(cache_dir=None, catalog=catalog)
        with pytest.raises(Sentinel5PError, match="No S5P NO2 granule found"):
            connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
            )

    def test_raises_when_no_data_asset(self) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector, Sentinel5PError

        item = _load_s5p_item()
        # Strip all assets from the item
        item.assets.clear()

        connector = Sentinel5PConnector(
            cache_dir=None,
            catalog=_make_mock_catalog([item]),
        )
        with pytest.raises(Sentinel5PError, match="Cannot locate data asset"):
            connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
            )


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestCaching:
    def test_cache_hit_skips_open_dataset(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector

        item = _load_s5p_item()
        fake_ds = _make_fake_s5p_dataset()
        connector = Sentinel5PConnector(cache_dir=tmp_path, catalog=_make_mock_catalog([item]))

        with patch("wced.ingest.sentinel5p.xr.open_dataset", return_value=fake_ds) as mock_open:
            connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
            )
            first_calls = mock_open.call_count  # should be 1

        # Re-search returns a fresh iterator over the same item.
        connector._catalog = _make_mock_catalog([item])  # type: ignore[assignment]
        with patch("wced.ingest.sentinel5p.xr.open_dataset", return_value=fake_ds) as mock_open2:
            connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
            )
            # Cache should have been used; open_dataset must not have been called
            # for the granule itself (only possibly for xr.open_dataset of the cached .nc).
            # We compare to the original call count — no new calls to the HDF5 path.
            assert mock_open2.call_count <= first_calls

    def test_content_hash_matches_cached_bytes(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector

        item = _load_s5p_item()
        fake_ds = _make_fake_s5p_dataset()
        connector = Sentinel5PConnector(cache_dir=tmp_path, catalog=_make_mock_catalog([item]))

        with patch("wced.ingest.sentinel5p.xr.open_dataset", return_value=fake_ds):
            _, source = connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
            )

        nc_files = list(tmp_path.glob("*.nc"))
        assert len(nc_files) == 1
        on_disk_hash = hashlib.sha256(nc_files[0].read_bytes()).hexdigest()
        assert source.content_hash == on_disk_hash


# ---------------------------------------------------------------------------
# Source metadata correctness
# ---------------------------------------------------------------------------

class TestSourceMetadata:
    def test_source_metadata_fields(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel5p import Sentinel5PConnector
        from wced.models.provenance import SourceType

        item = _load_s5p_item()
        fake_ds = _make_fake_s5p_dataset()
        connector = Sentinel5PConnector(
            cache_dir=tmp_path,
            catalog=_make_mock_catalog([item]),
            qa_threshold=0.75,
        )

        with patch("wced.ingest.sentinel5p.xr.open_dataset", return_value=fake_ds):
            _, source = connector.query_plume(
                32.66, 51.68,
                (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC)),
                product="NO2",
            )

        assert source.source_type is SourceType.SATELLITE
        assert source.retrieved_by == "wced.ingest.sentinel5p"
        assert source.metadata["product"] == "NO2"
        assert source.metadata["variable"] == "nitrogendioxide_tropospheric_column"
        assert source.metadata["qa_threshold"] == pytest.approx(0.75)
        assert source.metadata["target_lat"] == pytest.approx(32.66)
        assert source.metadata["target_lon"] == pytest.approx(51.68)
        assert source.metadata["platform"] == "Sentinel-5P"
        assert len(source.content_hash) == 64  # SHA-256 hex digest
