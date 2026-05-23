"""Sentinel-2 L2A ingest connector via Microsoft Planetary Computer STAC.

Searches the PC STAC catalog for Sentinel-2 L2A scenes near a point, then
downloads spatial chips (sub-scene windows) as xarray Datasets. Each chip
fetch produces a ``Source`` record for the provenance DAG.

Chip files are cached under ``cache_dir`` with a SHA-256 content hash in the
filename — the same item + bbox + bands combination never triggers a second
download.

External dependencies: pystac-client, planetary-computer, rioxarray, xarray.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pystac
import pystac_client
import planetary_computer
import rioxarray  # noqa: F401 — registers the rioxarray accessor on xr.DataArray
import xarray as xr

from wced.ingest.base import BBox
from wced.models.provenance import Source, SourceType

log = logging.getLogger(__name__)

_PC_STAC_URL: Final[str] = "https://planetarycomputer.microsoft.com/api/stac/v1"
_S2_COLLECTION: Final[str] = "sentinel-2-l2a"
_CLOUD_COVER_PROP: Final[str] = "eo:cloud_cover"
# L2A BOA surface-reflectance scale factor: DN → unitless [0, 1]
_S2_SCALE: Final[float] = 1.0 / 10_000.0

DEFAULT_BANDS: Final[tuple[str, ...]] = ("B04", "B03", "B02", "B12")
"""Default bands fetched by ``fetch_chip``: Red, Green, Blue, SWIR-1."""


class Sentinel2Error(RuntimeError):
    """Raised when Sentinel-2 retrieval fails irrecoverably."""


def _cloud_cover(item: pystac.Item) -> float:
    """Return ``eo:cloud_cover`` for a STAC item; falls back to 100.0 if absent."""
    return float(item.properties.get(_CLOUD_COVER_PROP, 100.0))


def _self_href(item: pystac.Item) -> str:
    """Return the self-link href for an item, falling back to the item ID."""
    href = item.get_self_href()
    return href if href is not None else item.id


def _chip_cache_key(item_id: str, bbox: BBox, bands: Sequence[str]) -> str:
    """Deterministic 32-char hex key for a (item, bbox, bands) combination."""
    payload = f"{item_id}|{','.join(map(str, bbox))}|{','.join(sorted(bands))}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _build_search_geometry(lat: float, lon: float, delta: float = 0.05) -> dict[str, Any]:
    """GeoJSON polygon centred on (lat, lon) for STAC ``intersects`` queries."""
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - delta, lat - delta],
            [lon + delta, lat - delta],
            [lon + delta, lat + delta],
            [lon - delta, lat + delta],
            [lon - delta, lat - delta],
        ]],
    }


class Sentinel2Connector:
    """Sentinel-2 L2A ingest connector via Microsoft Planetary Computer STAC.

    Parameters
    ----------
    cache_dir : Path or None
        Local directory for cached chip NetCDF files.  Defaults to
        ``~/.cache/wced/sentinel2``.  Pass ``None`` to disable caching (useful
        in tests where you want deterministic behaviour without touching disk).
    stac_url : str
        STAC API root URL.  Override in tests or to point at a mirror.
    catalog : pystac_client.Client or None
        Pre-built client injected by tests to avoid network calls.
    """

    name: str = "sentinel2"

    def __init__(
        self,
        cache_dir: Path | None = Path.home() / ".cache" / "wced" / "sentinel2",
        *,
        stac_url: str = _PC_STAC_URL,
        catalog: pystac_client.Client | None = None,
    ) -> None:
        self._cache_dir = cache_dir
        self._stac_url = stac_url
        self._catalog = catalog

    @property
    def _client(self) -> pystac_client.Client:
        if self._catalog is None:
            self._catalog = pystac_client.Client.open(
                self._stac_url,
                modifier=planetary_computer.sign_inplace,
            )
        return self._catalog

    # ------------------------------------------------------------------ public

    def search_around(
        self,
        lat: float,
        lon: float,
        datetime_window: tuple[datetime, datetime],
        max_cloud_pct: float = 20.0,
    ) -> list[pystac.Item]:
        """Return Sentinel-2 L2A scenes near a point, sorted by cloud cover.

        Parameters
        ----------
        lat, lon : float
            WGS84 target coordinates (decimal degrees).
        datetime_window : (start, end)
            Inclusive UTC-aware time window to search.
        max_cloud_pct : float
            Cloud-cover ceiling for the primary query.  If no scene meets it,
            the method falls back to all scenes and warns, returning the
            best-available item rather than raising.

        Returns
        -------
        list[pystac.Item]
            Items sorted ascending by ``eo:cloud_cover``.  Empty list only if
            the catalog contains no scenes intersecting the point+window at all.
        """
        start, end = datetime_window
        dt_str = f"{start.isoformat()}/{end.isoformat()}"
        geom = _build_search_geometry(lat, lon)

        search = self._client.search(
            collections=[_S2_COLLECTION],
            intersects=geom,
            datetime=dt_str,
            query={_CLOUD_COVER_PROP: {"lt": max_cloud_pct}},
        )
        items: list[pystac.Item] = list(search.items())

        if not items:
            # Relax the cloud filter and warn — never silently return nothing
            # when scenes exist; callers decide whether to accept high-cloud data.
            fallback = self._client.search(
                collections=[_S2_COLLECTION],
                intersects=geom,
                datetime=dt_str,
            )
            items = list(fallback.items())
            if items:
                items.sort(key=_cloud_cover)
                log.warning(
                    "sentinel2: no scene ≤%.0f%% cloud at (%.4f, %.4f) in %s; "
                    "returning best available (%.0f%% cloud, item=%s)",
                    max_cloud_pct,
                    lat,
                    lon,
                    dt_str,
                    _cloud_cover(items[0]),
                    items[0].id,
                )
            else:
                log.warning(
                    "sentinel2: no scenes at all for (%.4f, %.4f) in %s",
                    lat,
                    lon,
                    dt_str,
                )
            return items

        items.sort(key=_cloud_cover)
        return items

    def fetch_chip(
        self,
        item: pystac.Item,
        bbox: BBox,
        bands: Sequence[str] = DEFAULT_BANDS,
    ) -> tuple[xr.Dataset, Source]:
        """Download a spatial chip clipped to ``bbox`` from a Sentinel-2 item.

        Reads each band's COG asset, reprojects to EPSG:4326, clips to ``bbox``,
        and returns all bands as a single ``xr.Dataset``.  Surface reflectance
        values are scaled to [0, 1] (L2A BOA DN / 10 000).

        Chips are cached locally; the same item+bbox+bands combination is served
        from cache without re-fetching the COG.

        Parameters
        ----------
        item : pystac.Item
            A signed Planetary Computer STAC item (from ``search_around``).
        bbox : BBox
            ``(west, south, east, north)`` clip window in WGS84 (EPSG:4326).
        bands : sequence of str
            Asset keys to fetch.  Defaults to B04 (Red), B03 (Green), B02
            (Blue), B12 (SWIR-1).

        Returns
        -------
        (xr.Dataset, Source)
            ``Dataset`` with one ``float32`` variable per band (dims: y, x)
            in EPSG:4326.  ``Source`` records item ID, cloud cover, and the
            SHA-256 content hash of the cached NetCDF.

        Raises
        ------
        Sentinel2Error
            If a requested band is absent from the item's assets.
        """
        band_list = list(bands)
        cache_key = _chip_cache_key(item.id, bbox, band_list)

        if self._cache_dir is not None:
            cached_nc = self._cache_dir / f"{cache_key}.nc"
            if cached_nc.exists():
                log.debug("sentinel2: cache hit %s", cached_nc)
                ds = xr.open_dataset(cached_nc)
                return ds, self._build_source(item, bbox, band_list, cached_nc.read_bytes())

        missing = [b for b in band_list if b not in item.assets]
        if missing:
            raise Sentinel2Error(
                f"Bands {missing!r} not found in item {item.id!r}. "
                f"Available: {sorted(item.assets)}"
            )

        west, south, east, north = bbox
        arrays: dict[str, xr.DataArray] = {}
        for band in band_list:
            href = item.assets[band].href
            # Open as a rioxarray DataArray; squeeze drops the singleton band dim.
            da: xr.DataArray = rioxarray.open_rasterio(href, masked=True).squeeze(
                "band", drop=True
            )
            # clip_box with crs="EPSG:4326" transforms the bbox to the native
            # UTM CRS before windowing — avoids loading the full scene.
            clipped = da.rio.clip_box(
                minx=west, miny=south, maxx=east, maxy=north, crs="EPSG:4326"
            )
            reprojected = clipped.rio.reproject("EPSG:4326")
            arrays[band] = (reprojected * _S2_SCALE).astype("float32")

        ds = xr.Dataset(arrays)
        raw_bytes = ds.to_netcdf()  # in-memory NetCDF for hashing + cache write
        content_hash = hashlib.sha256(raw_bytes).hexdigest()

        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            # Save under cache_key so the lookup path and write path match.
            # content_hash is tracked in the Source record for integrity.
            out_path = self._cache_dir / f"{cache_key}.nc"
            out_path.write_bytes(raw_bytes)
            log.debug("sentinel2: cached chip → %s", out_path)

        return ds, self._build_source(item, bbox, band_list, raw_bytes)

    # ----------------------------------------------------------------- private

    def _build_source(
        self,
        item: pystac.Item,
        bbox: BBox,
        bands: list[str],
        raw_bytes: bytes,
    ) -> Source:
        return Source(
            source_type=SourceType.SATELLITE,
            identifier=_self_href(item),
            retrieved_at=datetime.now(tz=UTC),
            retrieved_by="wced.ingest.sentinel2",
            content_hash=hashlib.sha256(raw_bytes).hexdigest(),
            metadata={
                "stac_collection": _S2_COLLECTION,
                "platform": item.properties.get("platform", ""),
                "datetime": (
                    item.datetime.isoformat() if item.datetime is not None else None
                ),
                "cloud_cover": item.properties.get(_CLOUD_COVER_PROP),
                "bbox": list(bbox),
                "bands": bands,
            },
        )
