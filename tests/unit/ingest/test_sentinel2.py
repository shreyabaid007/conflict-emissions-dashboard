"""Tests for wced.ingest.sentinel2.

All STAC catalog and rioxarray calls are mocked — no network or filesystem I/O
happens.  STAC item fixtures live in tests/fixtures/stac_items/ as plain JSON
and are loaded with pystac.Item.from_dict().
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import xarray as xr

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "stac_items"

# ---------------------------------------------------------------------------
# Helpers to load fixture STAC items without a live catalog
# ---------------------------------------------------------------------------

def _load_s2_item(filename: str = "s2_isfahan_item.json"):
    import pystac  # noqa: PLC0415
    data = json.loads((FIXTURE_DIR / filename).read_text())
    return pystac.Item.from_dict(data)


def _make_rioxarray_open_mock(shape: tuple[int, int] = (4, 4), fill: float = 5000.0):
    """Return a side_effect callable for patch('rioxarray.open_rasterio').

    rioxarray registers ``rio`` as a read-only accessor property on xr.DataArray,
    so we cannot monkey-patch it after construction.  Instead we build a full
    MagicMock chain that ends with a real DataArray so arithmetic and .astype()
    in the connector work correctly.

    Chain: open_rasterio(href) → .squeeze() → .rio.clip_box() → .rio.reproject() → real DataArray
    """
    real_da = xr.DataArray(
        np.full(shape, fill, dtype="float32"),
        dims=["y", "x"],
        coords={
            "y": np.linspace(32.0, 32.4, shape[0]),
            "x": np.linspace(51.0, 51.4, shape[1]),
        },
    )

    # Build mock for the "with band dimension" return value of open_rasterio.
    squeezed = MagicMock(name="squeezed_da")
    squeezed.rio.clip_box.return_value = MagicMock(
        name="clipped",
        **{"rio.reproject.return_value": real_da},
    )

    opened = MagicMock(name="opened_da")
    opened.squeeze.return_value = squeezed

    return lambda href, **kw: opened


def _make_fake_da(shape: tuple[int, int] = (4, 4), fill: float = 5000.0) -> xr.DataArray:
    """Return a plain DataArray (no rioxarray accessor methods) for use as the
    terminal value of the open_rasterio mock chain."""
    return xr.DataArray(
        np.full(shape, fill, dtype="float32"),
        dims=["y", "x"],
        coords={"y": np.linspace(32.0, 32.4, shape[0]), "x": np.linspace(51.0, 51.4, shape[1])},
    )


def _make_mock_catalog(items: list[Any]) -> MagicMock:
    """Return a mock pystac_client.Client whose search().items() returns ``items``."""
    search_mock = MagicMock()
    search_mock.items.return_value = iter(items)
    catalog = MagicMock()
    catalog.search.return_value = search_mock
    return catalog


# ---------------------------------------------------------------------------
# search_around — cloud filtering logic
# ---------------------------------------------------------------------------

class TestSearchAround:
    """Cloud-cover filtering and fallback behaviour."""

    def test_returns_items_sorted_by_cloud(self) -> None:
        import pystac  # noqa: PLC0415
        from wced.ingest.sentinel2 import Sentinel2Connector

        low = _load_s2_item("s2_isfahan_item.json")   # 12.3 %
        high = _load_s2_item("s2_high_cloud_item.json")  # 78.5 %

        # Simulate catalog returning low-cloud item only (already below 20 %).
        connector = Sentinel2Connector(
            cache_dir=None,
            catalog=_make_mock_catalog([low]),
        )
        dt_window = (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 16, tzinfo=UTC))
        results = connector.search_around(32.66, 51.68, dt_window)

        assert results == [low]
        assert results[0].properties["eo:cloud_cover"] == pytest.approx(12.3)

    def test_falls_back_when_all_items_exceed_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging
        from wced.ingest.sentinel2 import Sentinel2Connector

        high = _load_s2_item("s2_high_cloud_item.json")  # 78.5 %

        # Primary search (cloud-filtered) returns nothing; fallback returns high-cloud item.
        primary_search = MagicMock()
        primary_search.items.return_value = iter([])
        fallback_search = MagicMock()
        fallback_search.items.return_value = iter([high])

        catalog = MagicMock()
        catalog.search.side_effect = [primary_search, fallback_search]

        connector = Sentinel2Connector(cache_dir=None, catalog=catalog)
        dt_window = (datetime(2026, 3, 16, tzinfo=UTC), datetime(2026, 3, 16, tzinfo=UTC))

        with caplog.at_level(logging.WARNING, logger="wced.ingest.sentinel2"):
            results = connector.search_around(32.66, 51.68, dt_window, max_cloud_pct=20.0)

        assert results == [high]
        assert any("best available" in r.message for r in caplog.records)

    def test_returns_empty_list_with_warning_when_no_scenes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        from wced.ingest.sentinel2 import Sentinel2Connector

        empty_search = MagicMock()
        empty_search.items.return_value = iter([])

        catalog = MagicMock()
        catalog.search.return_value = empty_search

        connector = Sentinel2Connector(cache_dir=None, catalog=catalog)
        dt_window = (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC))

        with caplog.at_level(logging.WARNING, logger="wced.ingest.sentinel2"):
            results = connector.search_around(32.66, 51.68, dt_window)

        assert results == []
        assert any("no scenes" in r.message for r in caplog.records)

    def test_items_sorted_ascending_by_cloud(self) -> None:
        from wced.ingest.sentinel2 import Sentinel2Connector

        low = _load_s2_item("s2_isfahan_item.json")    # 12.3 %
        high = _load_s2_item("s2_high_cloud_item.json")  # 78.5 %

        catalog = _make_mock_catalog([high, low])  # deliberately reversed
        connector = Sentinel2Connector(cache_dir=None, catalog=catalog)
        dt_window = (datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 3, 16, tzinfo=UTC))

        results = connector.search_around(32.66, 51.68, dt_window, max_cloud_pct=90.0)
        cloud_values = [r.properties["eo:cloud_cover"] for r in results]
        assert cloud_values == sorted(cloud_values)


# ---------------------------------------------------------------------------
# fetch_chip — dataset structure and Source record
# ---------------------------------------------------------------------------

class TestFetchChip:
    """fetch_chip output shape, scaling, and provenance."""

    def test_returns_dataset_with_one_var_per_band(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel2 import Sentinel2Connector

        item = _load_s2_item()
        connector = Sentinel2Connector(cache_dir=tmp_path, catalog=_make_mock_catalog([item]))

        with patch("wced.ingest.sentinel2.rioxarray.open_rasterio",
                   side_effect=_make_rioxarray_open_mock()):
            ds, _ = connector.fetch_chip(item, (51.0, 32.0, 52.0, 33.0))

        assert set(ds.data_vars) == {"B04", "B03", "B02", "B12"}

    def test_reflectance_scaled_to_0_1(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel2 import Sentinel2Connector

        item = _load_s2_item()
        connector = Sentinel2Connector(cache_dir=tmp_path, catalog=_make_mock_catalog([item]))

        # fill=5000 DN → 0.5 after ÷10000
        with patch("wced.ingest.sentinel2.rioxarray.open_rasterio",
                   side_effect=_make_rioxarray_open_mock(fill=5000.0)):
            ds, _ = connector.fetch_chip(item, (51.0, 32.0, 52.0, 33.0))

        assert float(ds["B04"].values.max()) == pytest.approx(0.5, abs=1e-4)

    def test_raises_on_missing_band(self) -> None:
        from wced.ingest.sentinel2 import Sentinel2Connector, Sentinel2Error

        item = _load_s2_item()
        connector = Sentinel2Connector(cache_dir=None, catalog=_make_mock_catalog([item]))

        with pytest.raises(Sentinel2Error, match="B99"):
            connector.fetch_chip(item, (51.0, 32.0, 52.0, 33.0), bands=["B04", "B99"])

    def test_source_record_fields(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel2 import Sentinel2Connector
        from wced.models.provenance import SourceType

        item = _load_s2_item()
        connector = Sentinel2Connector(cache_dir=tmp_path, catalog=_make_mock_catalog([item]))

        with patch("wced.ingest.sentinel2.rioxarray.open_rasterio",
                   side_effect=_make_rioxarray_open_mock()):
            _, source = connector.fetch_chip(item, (51.0, 32.0, 52.0, 33.0))

        assert source.source_type is SourceType.SATELLITE
        assert source.retrieved_by == "wced.ingest.sentinel2"
        assert source.identifier.endswith(item.id)
        assert source.metadata["cloud_cover"] == pytest.approx(12.3)
        assert source.metadata["bbox"] == [51.0, 32.0, 52.0, 33.0]
        assert "B04" in source.metadata["bands"]
        assert len(source.content_hash) == 64  # SHA-256 hex

    def test_source_content_hash_is_sha256_of_netcdf(self, tmp_path: Path) -> None:
        from wced.ingest.sentinel2 import Sentinel2Connector

        item = _load_s2_item()
        connector = Sentinel2Connector(cache_dir=tmp_path, catalog=_make_mock_catalog([item]))

        with patch("wced.ingest.sentinel2.rioxarray.open_rasterio",
                   side_effect=_make_rioxarray_open_mock()):
            _, source = connector.fetch_chip(item, (51.0, 32.0, 52.0, 33.0))

        # The content hash must match the cached NetCDF bytes on disk.
        cached_files = list(tmp_path.glob("*.nc"))
        assert len(cached_files) == 1
        on_disk_hash = hashlib.sha256(cached_files[0].read_bytes()).hexdigest()
        assert source.content_hash == on_disk_hash

    def test_cache_hit_skips_rioxarray(self, tmp_path: Path) -> None:
        """Second fetch_chip call for same item returns cached Dataset without re-reading COGs."""
        from wced.ingest.sentinel2 import Sentinel2Connector

        item = _load_s2_item()
        connector = Sentinel2Connector(cache_dir=tmp_path, catalog=_make_mock_catalog([item]))

        with patch("wced.ingest.sentinel2.rioxarray.open_rasterio",
                   side_effect=_make_rioxarray_open_mock()):
            connector.fetch_chip(item, (51.0, 32.0, 52.0, 33.0))

        # Second call — open_rasterio should NOT be called again (cache hit).
        with patch("wced.ingest.sentinel2.rioxarray.open_rasterio") as mock_open2:
            connector.fetch_chip(item, (51.0, 32.0, 52.0, 33.0))
            assert mock_open2.call_count == 0, "cache miss on second call — caching broken"


# ---------------------------------------------------------------------------
# cache key determinism
# ---------------------------------------------------------------------------

class TestCacheKey:
    def test_same_inputs_produce_same_key(self) -> None:
        from wced.ingest.sentinel2 import _chip_cache_key

        key1 = _chip_cache_key("item-abc", (51.0, 32.0, 52.0, 33.0), ["B04", "B12"])
        key2 = _chip_cache_key("item-abc", (51.0, 32.0, 52.0, 33.0), ["B12", "B04"])
        # bands are sorted in the key so order does not matter
        assert key1 == key2

    def test_different_item_id_produces_different_key(self) -> None:
        from wced.ingest.sentinel2 import _chip_cache_key

        key1 = _chip_cache_key("item-abc", (51.0, 32.0, 52.0, 33.0), ["B04"])
        key2 = _chip_cache_key("item-xyz", (51.0, 32.0, 52.0, 33.0), ["B04"])
        assert key1 != key2

    def test_different_bbox_produces_different_key(self) -> None:
        from wced.ingest.sentinel2 import _chip_cache_key

        key1 = _chip_cache_key("item-abc", (51.0, 32.0, 52.0, 33.0), ["B04"])
        key2 = _chip_cache_key("item-abc", (51.1, 32.0, 52.0, 33.0), ["B04"])
        assert key1 != key2
