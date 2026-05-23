"""NASA FIRMS active-fire connector.

NASA's Fire Information for Resource Management System distributes per-pixel
thermal anomaly detections from MODIS (Terra, Aqua) and VIIRS (S-NPP, NOAA-20,
NOAA-21) sensors. WCED uses it as the high-cadence seed for fire-event
detection over the Iran + Gulf AOI; the detections are then attributed to
registered facilities downstream in ``wced.detect``.

API documentation
-----------------
Area API:        https://firms.modaps.eosdis.nasa.gov/api/area/
MAP_KEY signup:  https://firms.modaps.eosdis.nasa.gov/api/map_key/
Attribution:     https://firms.modaps.eosdis.nasa.gov/

Each request returns CSV. Column sets differ between MODIS and VIIRS — see the
``_MODIS_BRIGHTNESS_COL`` / ``_VIIRS_BRIGHTNESS_COL`` constants below. The
connector normalises both into a single ``brightness`` field (Kelvin) so
downstream code does not need to know which sensor produced the detection.

Rate limit
----------
FIRMS does not publish a hard per-key limit but the docs ask consumers to be
polite. We cap concurrency by issuing one request at a time per connector
instance and back off exponentially on 5xx / network errors.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wced.ingest.base import BBox
from wced.models.provenance import Source, SourceType

log = logging.getLogger(__name__)

# Base URL for the area-CSV endpoint. The full URL is built by appending
# `/{MAP_KEY}/{source}/{w,s,e,n}/{day_range}/{date}`.
FIRMS_AREA_CSV_BASE: Final[str] = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# FIRMS area API caps a single request at 10 days. Longer windows are split.
_MAX_DAY_RANGE: Final[int] = 10

# FIRMS source identifiers. See https://firms.modaps.eosdis.nasa.gov/api/area/.
VIIRS_SOURCES: Final[tuple[str, ...]] = (
    "VIIRS_SNPP_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_NOAA21_NRT",
)
MODIS_SOURCES: Final[tuple[str, ...]] = ("MODIS_NRT",)

# Brightness column names differ by sensor; we normalise to "brightness" (K).
_VIIRS_BRIGHTNESS_COL: Final[str] = "bright_ti4"
_MODIS_BRIGHTNESS_COL: Final[str] = "brightness"

# Numeric fields we coerce out of CSV strings. Confidence is text for VIIRS
# ("l"/"n"/"h") and numeric for MODIS, so it is kept as a string and parsed
# downstream.
_FLOAT_FIELDS: Final[tuple[str, ...]] = ("latitude", "longitude", "brightness", "scan", "track", "frp")


class FIRMSError(RuntimeError):
    """Raised when the FIRMS API returns a non-recoverable error response."""


def _content_hash(body: bytes) -> str:
    """SHA-256 hex digest of a raw response body."""
    return hashlib.sha256(body).hexdigest()


def _parse_acq_datetime(acq_date: str, acq_time: str) -> datetime:
    """Combine FIRMS ``acq_date`` (YYYY-MM-DD) and ``acq_time`` (HHMM) → UTC.

    FIRMS documents all timestamps as UTC. ``acq_time`` is a zero-padded
    four-digit string like "0314" meaning 03:14 UTC. Some historical rows
    omit the leading zero, so we left-pad defensively.
    """
    padded = acq_time.strip().zfill(4)
    hh, mm = int(padded[:2]), int(padded[2:])
    d = date.fromisoformat(acq_date.strip())
    return datetime.combine(d, time(hh, mm), tzinfo=UTC)


def _coerce_record(row: dict[str, str], brightness_col: str) -> dict[str, object]:
    """Normalise one CSV row into the connector's output shape.

    The brightness column is sensor-dependent (``bright_ti4`` for VIIRS,
    ``brightness`` for MODIS). We rename it to ``brightness`` and drop the
    original key so callers do not have to sniff the sensor.
    """
    out: dict[str, object] = dict(row)
    if brightness_col != "brightness":
        out["brightness"] = out.pop(brightness_col, "")
    for field in _FLOAT_FIELDS:
        raw = out.get(field, "")
        if raw == "" or raw is None:
            continue
        try:
            out[field] = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            log.warning("firms: non-numeric %s=%r — leaving as string", field, raw)
    out["detected_at"] = _parse_acq_datetime(str(row["acq_date"]), str(row["acq_time"]))
    return out


def _iter_chunks(start: datetime, end: datetime) -> Iterable[tuple[date, int]]:
    """Yield (anchor_date, day_range) chunks covering [start, end] in ≤10-day spans.

    The FIRMS area endpoint is parameterised by a *trailing* day range anchored
    at a single date — the query returns detections for the ``day_range`` days
    *ending* on the anchor date. We slice the window forward in 10-day blocks
    from start.date() and emit one (anchor, span) per block.
    """
    if end < start:
        raise ValueError(f"end ({end}) must be >= start ({start})")
    cur = start.date()
    last = end.date()
    while cur <= last:
        span_end = min(cur + timedelta(days=_MAX_DAY_RANGE - 1), last)
        span = (span_end - cur).days + 1
        yield span_end, span
        cur = span_end + timedelta(days=1)


class FIRMSConnector:
    """Async ingest connector for the NASA FIRMS area CSV endpoint.

    One instance is bound to a single MAP_KEY. The connector exposes both
    ``ingest_viirs`` and ``ingest_modis`` as separate async generators —
    downstream callers usually want the sensor stream tagged so they can apply
    different persistence rules (VIIRS is 375 m; MODIS is 1 km).
    """

    name: str = "firms"

    def __init__(
        self,
        map_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = FIRMS_AREA_CSV_BASE,
        request_timeout: float = 30.0,
        max_attempts: int = 5,
    ) -> None:
        if not map_key:
            raise ValueError("FIRMS map_key must be a non-empty string")
        self._map_key = map_key
        self._base_url = base_url.rstrip("/")
        self._timeout = request_timeout
        self._max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> FIRMSConnector:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ public

    async def ingest_viirs(
        self,
        start: datetime,
        end: datetime,
        bbox: BBox,
    ) -> AsyncIterator[dict]:
        """Yield VIIRS detections (S-NPP + NOAA-20 + NOAA-21) in [start, end] ∩ bbox."""
        async for record in self._ingest_sources(VIIRS_SOURCES, start, end, bbox, _VIIRS_BRIGHTNESS_COL):
            yield record

    async def ingest_modis(
        self,
        start: datetime,
        end: datetime,
        bbox: BBox,
    ) -> AsyncIterator[dict]:
        """Yield MODIS (Terra+Aqua) detections in [start, end] ∩ bbox."""
        async for record in self._ingest_sources(MODIS_SOURCES, start, end, bbox, _MODIS_BRIGHTNESS_COL):
            yield record

    async def ingest(
        self,
        start: datetime,
        end: datetime,
        bbox: BBox,
    ) -> AsyncIterator[dict]:
        """IngestConnector entrypoint — yields VIIRS then MODIS in one stream.

        Most callers should prefer the sensor-specific methods so they can tag
        records with the originating ``DetectionSource`` enum value.
        """
        async for record in self.ingest_viirs(start, end, bbox):
            yield record
        async for record in self.ingest_modis(start, end, bbox):
            yield record

    # ------------------------------------------------------------------ internal

    async def _ingest_sources(
        self,
        sources: Iterable[str],
        start: datetime,
        end: datetime,
        bbox: BBox,
        brightness_col: str,
    ) -> AsyncIterator[dict]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("FIRMS connector requires timezone-aware start/end")
        for source in sources:
            for anchor, span in _iter_chunks(start, end):
                async for record in self._fetch_one(source, bbox, anchor, span, brightness_col):
                    yield record

    async def _fetch_one(
        self,
        source: str,
        bbox: BBox,
        anchor: date,
        day_range: int,
        brightness_col: str,
    ) -> AsyncIterator[dict]:
        url = self._build_url(source, bbox, anchor, day_range)
        body = await self._get_with_retry(url)
        source_record = Source(
            source_type=SourceType.SATELLITE,
            identifier=url,
            retrieved_at=datetime.now(tz=UTC),
            retrieved_by=f"wced.ingest.firms:{source}",
            content_hash=_content_hash(body),
            metadata={
                "firms_source": source,
                "bbox": list(bbox),
                "anchor_date": anchor.isoformat(),
                "day_range": day_range,
            },
        )
        for row in self._parse_csv(body):
            record = _coerce_record(row, brightness_col)
            record["_source"] = source_record
            yield record

    def _build_url(self, source: str, bbox: BBox, anchor: date, day_range: int) -> str:
        west, south, east, north = bbox
        coords = f"{west},{south},{east},{north}"
        return (
            f"{self._base_url}/{self._map_key}/{source}/{coords}/{day_range}/"
            f"{anchor.isoformat()}"
        )

    async def _get_with_retry(self, url: str) -> bytes:
        if self._client is None:
            raise RuntimeError(
                "FIRMSConnector must be used as an async context manager "
                "or initialised with an explicit httpx.AsyncClient"
            )
        client = self._client
        # Retry on transient transport errors and 5xx responses. 4xx is fatal
        # (bad MAP_KEY, bad bbox) and should bubble up immediately.
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=10.0),
            retry=retry_if_exception_type((httpx.TransportError, _RetryableStatus)),
            reraise=True,
        ):
            with attempt:
                response = await client.get(url)
                if response.status_code >= 500:
                    log.warning(
                        "firms: %s returned %d, retrying", url, response.status_code
                    )
                    raise _RetryableStatus(response.status_code)
                if response.status_code >= 400:
                    raise FIRMSError(
                        f"FIRMS request failed: {response.status_code} {response.text[:200]}"
                    )
                return response.content
        raise RuntimeError("unreachable")  # pragma: no cover

    @staticmethod
    def _parse_csv(body: bytes) -> Iterable[dict[str, str]]:
        text = body.decode("utf-8", errors="replace")
        # FIRMS occasionally returns an HTML error page; guard against that
        # rather than feeding it to DictReader and producing nonsense rows.
        if text.lstrip().startswith("<"):
            raise FIRMSError(f"FIRMS returned non-CSV payload: {text[:120]!r}")
        # An empty bbox/day-range returns just the header row, which DictReader
        # handles as zero rows naturally.
        reader = csv.DictReader(io.StringIO(text))
        yield from reader


class _RetryableStatus(Exception):
    """Internal marker exception that triggers a tenacity retry on 5xx."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"retryable status {status_code}")
        self.status_code = status_code
