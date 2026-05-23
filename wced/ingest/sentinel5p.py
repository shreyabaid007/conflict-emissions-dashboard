"""Sentinel-5P (TROPOMI) L2 plume ingest connector via Planetary Computer STAC.

Downloads TROPOMI L2 granules for NO2, CO, SO2, or CH4 near a lat/lon point,
spatially filters to a window around that point, and returns the relevant
column-density variable as an ``xr.Dataset``.  Each successful query produces
a ``Source`` record for the provenance DAG.

⚠️  BIAS WARNING (NO2)
----------------------
TROPOMI NO2 tropospheric column products (v2.x, collection 3) carry a
systematic −23 % low bias relative to independent observations in the lower
troposphere. Reference: van Geffen et al. (2022), *Atmos. Meas. Tech.* 15,
1915–1935, https://doi.org/10.5194/amt-15-1915-2022.

Callers **must not** divide reported column densities by this factor before
reaching the quantify layer — the correction must be applied with a recorded
Provenance step so the bias correction itself is auditable.  This module
surfaces the raw values and attaches a ``bias_warning`` field to the Source
metadata so downstream code cannot overlook it.

QA filtering
------------
TROPOMI ATBD §2.6 recommends ``qa_value ≥ 0.75`` for cloud-free scenes and
``qa_value ≥ 0.5`` for cloudy retrievals (snow/ice).  This connector applies
``qa_value ≥ 0.75`` by default, keeping only high-quality pixels.  Pass
``qa_threshold=0.5`` for scenes over snow-covered terrain.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import pystac
import pystac_client
import planetary_computer
import xarray as xr

from wced.ingest.base import BBox
from wced.models.provenance import Source, SourceType

log = logging.getLogger(__name__)

_PC_STAC_URL: Final[str] = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Planetary Computer collection IDs for each S5P L2 product.
_S5P_COLLECTIONS: Final[dict[str, str]] = {
    "NO2": "sentinel-5p-l2-no2",
    "CO": "sentinel-5p-l2-co",
    "SO2": "sentinel-5p-l2-so2",
    "CH4": "sentinel-5p-l2-ch4",
}

# HDF5 group that holds the main geophysical variables in every S5P L2 file.
_PRODUCT_GROUP: Final[str] = "PRODUCT"

# Variable name within the PRODUCT group for each supported product.
# Values are the short names used in both the HDF5 paths and output Dataset.
_S5P_VARIABLES: Final[dict[str, str]] = {
    "NO2": "nitrogendioxide_tropospheric_column",
    "CO": "carbonmonoxide_total_column",
    "SO2": "sulfurdioxide_total_vertical_column",
    "CH4": "methane_mixing_ratio_bias_corrected",
}

# QA value variable name (same across all products).
# 0.75 is the TROPOMI ATBD §2.6 recommendation for standard analyses.  Fresh
# combustion plumes can score lower due to aerosol interference; V2 plume-
# specific retrievals may need to relax this to ≥ 0.50.
_QA_VAR: Final[str] = "qa_value"
# Lat/lon variable names in the PRODUCT group.
_LAT_VAR: Final[str] = "latitude"
_LON_VAR: Final[str] = "longitude"

# Default spatial search window radius around the target point (degrees).
_DEFAULT_SEARCH_DELTA_DEG: Final[float] = 1.0

# TROPOMI NO2 v2.x tropospheric column bias (van Geffen et al. 2022 AMT §3).
_NO2_TROPOMI_BIAS: Final[float] = -0.23


class Sentinel5PError(RuntimeError):
    """Raised when an S5P retrieval fails irrecoverably."""


def _collection_for(product: str) -> str:
    prod_upper = product.upper()
    if prod_upper not in _S5P_COLLECTIONS:
        raise Sentinel5PError(
            f"Unknown S5P product {product!r}. "
            f"Supported: {sorted(_S5P_COLLECTIONS)}"
        )
    return _S5P_COLLECTIONS[prod_upper]


def _variable_for(product: str) -> str:
    return _S5P_VARIABLES[product.upper()]


def _build_search_bbox(lat: float, lon: float, delta: float) -> tuple[float, float, float, float]:
    return (lon - delta, lat - delta, lon + delta, lat + delta)


def _self_href(item: pystac.Item) -> str:
    href = item.get_self_href()
    return href if href is not None else item.id


def _granule_cache_key(item_id: str, lat: float, lon: float, product: str, delta: float) -> str:
    payload = f"{item_id}|{lat}|{lon}|{product.upper()}|{delta}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class Sentinel5PConnector:
    """TROPOMI L2 plume ingest connector via Microsoft Planetary Computer STAC.

    Each ``query_plume`` call returns the column-density variable for the
    requested product over a spatial window centred on the target point,
    with ``qa_value``-based filtering applied.

    Parameters
    ----------
    cache_dir : Path or None
        Local directory for cached NetCDF plume files.  Defaults to
        ``~/.cache/wced/sentinel5p``.  Pass ``None`` to disable (tests).
    stac_url : str
        STAC API root URL.  Override for mirrors or tests.
    catalog : pystac_client.Client or None
        Injected client for testing without network access.
    qa_threshold : float
        Minimum ``qa_value`` to retain.  Default 0.75 per TROPOMI ATBD §2.6.
    search_delta_deg : float
        Half-width of the spatial search box around the target point (degrees).
    """

    name: str = "sentinel5p"

    def __init__(
        self,
        cache_dir: Path | None = Path.home() / ".cache" / "wced" / "sentinel5p",
        *,
        stac_url: str = _PC_STAC_URL,
        catalog: pystac_client.Client | None = None,
        qa_threshold: float = 0.75,
        search_delta_deg: float = _DEFAULT_SEARCH_DELTA_DEG,
    ) -> None:
        self._cache_dir = cache_dir
        self._stac_url = stac_url
        self._catalog = catalog
        self._qa_threshold = qa_threshold
        self._search_delta_deg = search_delta_deg

    @property
    def _client(self) -> pystac_client.Client:
        if self._catalog is None:
            self._catalog = pystac_client.Client.open(
                self._stac_url,
                modifier=planetary_computer.sign_inplace,
            )
        return self._catalog

    # ------------------------------------------------------------------ public

    def query_plume(
        self,
        lat: float,
        lon: float,
        datetime_window: tuple[datetime, datetime],
        product: str = "NO2",
    ) -> tuple[xr.Dataset, Source]:
        """Return TROPOMI column-density pixels near a point for one product.

        Searches the PC STAC catalog for granules that intersect a bounding box
        around ``(lat, lon)`` in the given time window, opens the first (most
        recent) granule, spatially filters to the search box, and applies
        ``qa_value ≥ qa_threshold`` masking.

        ⚠️  NO2 BIAS: TROPOMI NO2 v2.x carries a −23 % tropospheric column bias
        (van Geffen et al. 2022 AMT).  The raw column values are returned here;
        callers must apply the correction via a recorded Provenance step.  The
        Source metadata field ``bias_warning`` is set for NO2 retrievals as a
        machine-readable flag.

        Parameters
        ----------
        lat, lon : float
            WGS84 target coordinates (decimal degrees).
        datetime_window : (start, end)
            Inclusive UTC-aware time window.
        product : str
            One of ``"NO2"``, ``"CO"``, ``"SO2"``, ``"CH4"`` (case-insensitive).

        Returns
        -------
        (xr.Dataset, Source)
            Dataset with variables ``{product_variable}``, ``qa_value``,
            ``latitude``, ``longitude`` — all indexed by ``(scanline, ground_pixel)``.
            QA-failed pixels are masked to ``NaN``.  Source records the granule
            ID, retrieval time, content hash, and bias warning if applicable.

        Raises
        ------
        Sentinel5PError
            If no granule covers the point+window, or the product is unknown.
        """
        prod_upper = product.upper()
        collection = _collection_for(prod_upper)
        variable = _variable_for(prod_upper)

        start, end = datetime_window
        dt_str = f"{start.isoformat()}/{end.isoformat()}"
        search_bbox = _build_search_bbox(lat, lon, self._search_delta_deg)

        cache_key = _granule_cache_key(
            # Include bbox corners in key so different windows stay separate.
            f"{search_bbox}",
            lat,
            lon,
            prod_upper,
            self._search_delta_deg,
        )

        search = self._client.search(
            collections=[collection],
            bbox=list(search_bbox),
            datetime=dt_str,
        )
        items: list[pystac.Item] = list(search.items())
        if not items:
            raise Sentinel5PError(
                f"No S5P {prod_upper} granule found at ({lat:.4f}, {lon:.4f}) "
                f"in {dt_str}"
            )

        # Use the first returned item (most recent within the window).
        item = items[0]

        if self._cache_dir is not None:
            cached_nc = self._cache_dir / f"{cache_key}.nc"
            if cached_nc.exists():
                log.debug("sentinel5p: cache hit %s", cached_nc)
                ds = xr.open_dataset(cached_nc)
                return ds, self._build_source(
                    item, lat, lon, prod_upper, variable, cached_nc.read_bytes()
                )

        ds = self._open_and_filter(item, variable, lat, lon)
        raw_bytes = ds.to_netcdf()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()

        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._cache_dir / f"{content_hash}.nc"
            out_path.write_bytes(raw_bytes)
            log.debug("sentinel5p: cached plume → %s", out_path)

        return ds, self._build_source(item, lat, lon, prod_upper, variable, raw_bytes)

    # ----------------------------------------------------------------- private

    def _open_and_filter(
        self,
        item: pystac.Item,
        variable: str,
        lat: float,
        lon: float,
    ) -> xr.Dataset:
        """Open the PRODUCT group from the granule HDF5 and spatially filter."""
        data_asset = self._data_asset_href(item)
        # S5P L2 files are HDF5 (NetCDF4-compatible); h5netcdf reads them.
        # The PRODUCT group contains the main geophysical variables and
        # 2-D lat/lon arrays — open only that group to avoid loading large
        # auxiliary datasets.
        ds_full: xr.Dataset = xr.open_dataset(
            data_asset,
            group=_PRODUCT_GROUP,
            engine="h5netcdf",
            mask_and_scale=True,
        )

        # Build a 2-D spatial mask inside the search bounding box.
        lats: xr.DataArray = ds_full[_LAT_VAR]
        lons: xr.DataArray = ds_full[_LON_VAR]
        delta = self._search_delta_deg
        spatial_mask: xr.DataArray = (
            (lats >= lat - delta)
            & (lats <= lat + delta)
            & (lons >= lon - delta)
            & (lons <= lon + delta)
        )

        # qa_value ≥ threshold mask (TROPOMI ATBD §2.6 recommendation).
        qa: xr.DataArray = ds_full[_QA_VAR]
        qa_mask: xr.DataArray = qa >= self._qa_threshold
        combined_mask = spatial_mask & qa_mask

        col = ds_full[variable].where(combined_mask)
        qa_out = qa.where(combined_mask)
        lats_out = lats.where(combined_mask)
        lons_out = lons.where(combined_mask)

        out = xr.Dataset(
            {
                variable: col,
                _QA_VAR: qa_out,
                _LAT_VAR: lats_out,
                _LON_VAR: lons_out,
            }
        )

        n_valid = int(np.sum(~np.isnan(col.values)))
        if n_valid == 0:
            log.warning(
                "sentinel5p: zero valid %s pixels near (%.4f, %.4f) after QA filter "
                "(qa_threshold=%.2f)",
                variable,
                lat,
                lon,
                self._qa_threshold,
            )

        return out

    def _data_asset_href(self, item: pystac.Item) -> str:
        """Return the HDF5 data asset href from a S5P STAC item.

        Tries common asset keys in priority order; raises if none are found.
        """
        for key in ("data", "product", "hdf5"):
            if key in item.assets:
                return item.assets[key].href
        # Fall back to the first asset that looks like an HDF5/NetCDF file.
        for key, asset in item.assets.items():
            href = asset.href
            if href.endswith((".nc", ".h5", ".hdf5")):
                return href
        raise Sentinel5PError(
            f"Cannot locate data asset in S5P item {item.id!r}. "
            f"Available assets: {sorted(item.assets)}"
        )

    def _build_source(
        self,
        item: pystac.Item,
        lat: float,
        lon: float,
        product: str,
        variable: str,
        raw_bytes: bytes,
    ) -> Source:
        metadata: dict[str, Any] = {
            "stac_collection": _S5P_COLLECTIONS[product],
            "platform": item.properties.get("platform", "Sentinel-5P"),
            "datetime": (
                item.datetime.isoformat() if item.datetime is not None else None
            ),
            "target_lat": lat,
            "target_lon": lon,
            "product": product,
            "variable": variable,
            "qa_threshold": self._qa_threshold,
            "search_delta_deg": self._search_delta_deg,
        }
        # Surface the NO2 bias prominently so downstream code cannot overlook
        # it.  van Geffen et al. (2022) AMT 15, 1915–1935.
        # Correction applied in wced.validate.tropomi per methodology §3.6
        if product == "NO2":
            metadata["bias_warning"] = (
                "TROPOMI NO2 v2.x tropospheric column has a systematic "
                f"{_NO2_TROPOMI_BIAS * 100:.0f}% bias (van Geffen et al. 2022 "
                "AMT doi:10.5194/amt-15-1915-2022). Apply correction via a "
                "recorded Provenance step before using in emission calculations."
            )

        return Source(
            source_type=SourceType.SATELLITE,
            identifier=_self_href(item),
            retrieved_at=datetime.now(tz=UTC),
            retrieved_by="wced.ingest.sentinel5p",
            content_hash=hashlib.sha256(raw_bytes).hexdigest(),
            metadata=metadata,
        )
