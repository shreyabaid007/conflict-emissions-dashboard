"""UCDP Georeferenced Event Dataset (GED) ingest connector.

The Uppsala Conflict Data Program (UCDP) is the world's main provider of
data on organised violence. UCDP GED provides georeferenced conflict events
with high spatial precision, updated annually (with candidate events published
quarterly).

**This connector is for historical validation and backfill only — not daily
ingestion.** UCDP data has months of latency and is not suitable for
near-real-time monitoring. Use it to cross-validate GDELT/ACLED detections
against an academically curated ground truth.

API reference
-------------
GED API:    https://ucdpapi.pcr.uu.se/api/gedevents/<version>
Candidate:  https://ucdpapi.pcr.uu.se/api/candidates/<version>
Docs:       https://ucdp.uu.se/apidocs/

No authentication is required.

Attribution (mandatory)
-----------------------
UCDP data is published under Creative Commons Attribution 4.0 (CC BY 4.0).
Outputs must carry this citation:

    Sundberg, Ralph, and Erik Melander, 2013, "Introducing the UCDP
    Georeferenced Event Dataset", Journal of Peace Research, vol.50, no.4,
    523-532.

The canonical form is stored in :data:`UCDP_ATTRIBUTION`.

Pagination
----------
The API returns at most ``pagesize`` rows per page (default 1000, max 1000).
The connector paginates via ``page`` parameter until
``TotalCount <= page * pagesize``.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any, Final

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wced.models.provenance import Source, SourceType

log = logging.getLogger(__name__)

UCDP_GED_API_URL: Final[str] = (
    "https://ucdpapi.pcr.uu.se/api/gedevents/24.0.10"
)

UCDP_ATTRIBUTION: Final[str] = (
    'Sundberg, Ralph, and Erik Melander, 2013, "Introducing the UCDP'
    " Georeferenced Event Dataset\", Journal of Peace Research, vol.50,"
    " no.4, 523-532."
)

UCDP_MAX_PAGESIZE: Final[int] = 1000

# UCDP type_of_violence codes relevant to infrastructure damage.
# 1 = state-based armed conflict, 2 = non-state conflict, 3 = one-sided violence.
RELEVANT_VIOLENCE_TYPES: Final[frozenset[int]] = frozenset({1, 2, 3})

# Country IDs for the 2026 conflict theatre (UCDP uses GW country codes).
DEFAULT_COUNTRY_IDS: Final[tuple[int, ...]] = (
    630,  # Iran
    666,  # Israel
)


class UCDPEvent(BaseModel):
    """Raw upstream UCDP GED event record.

    Field names mirror the UCDP GED API response. Mapping to WCED domain
    models is the responsibility of downstream verification modules.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(description="UCDP unique event identifier (id field)")
    date_start: date
    date_end: date
    type_of_violence: int
    conflict_name: str
    side_a: str
    side_b: str
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    country: str
    country_id: int
    region: str
    source_article: str
    best_est: int = Field(ge=0, description="Best estimate of fatalities")
    high_est: int = Field(ge=0, description="High estimate of fatalities")
    low_est: int = Field(ge=0, description="Low estimate of fatalities")
    where_prec: int = Field(
        ge=1, le=7,
        description="Precision of geolocation (1=exact, 7=country)",
    )
    date_prec: int = Field(
        ge=1, le=5,
        description="Precision of date (1=exact day, 5=year only)",
    )
    detected_at: AwareDatetime


class UCDPError(RuntimeError):
    """Raised when a UCDP API request returns a non-recoverable error."""


def _content_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _parse_ged_record(record: dict[str, Any]) -> UCDPEvent | None:
    """Parse a single record from the UCDP GED API response.

    Returns None if required geolocation fields are missing or invalid.
    """
    try:
        lat = float(record.get("latitude", 0))
        lon = float(record.get("longitude", 0))
    except (TypeError, ValueError):
        return None

    if lat == 0.0 and lon == 0.0:
        return None

    try:
        date_start = date.fromisoformat(str(record.get("date_start", "")))
        date_end = date.fromisoformat(str(record.get("date_end", "")))
    except ValueError:
        return None

    detected_at = datetime(
        date_start.year, date_start.month, date_start.day, tzinfo=UTC,
    )

    try:
        type_of_violence = int(record.get("type_of_violence", 0))
    except (TypeError, ValueError):
        return None

    return UCDPEvent(
        event_id=str(record.get("id", "")),
        date_start=date_start,
        date_end=date_end,
        type_of_violence=type_of_violence,
        conflict_name=str(record.get("conflict_name", "")),
        side_a=str(record.get("side_a", "")),
        side_b=str(record.get("side_b", "")),
        latitude=lat,
        longitude=lon,
        country=str(record.get("country", "")),
        country_id=int(record.get("country_id", 0)),
        region=str(record.get("region", "")),
        source_article=str(record.get("source_article", "")),
        best_est=int(record.get("best", 0)),
        high_est=int(record.get("high", 0)),
        low_est=int(record.get("low", 0)),
        where_prec=int(record.get("where_prec", 7)),
        date_prec=int(record.get("date_prec", 5)),
        detected_at=detected_at,
    )


class UCDPConnector:
    """Async ingest connector for UCDP Georeferenced Event Dataset.

    Queries the UCDP GED API for conflict events within a time window,
    paginating automatically. No authentication required.

    This connector is intended for **historical validation and backfill
    only** — UCDP data has months of latency and is not suitable for
    daily near-real-time ingestion.

    Parameters
    ----------
    client : httpx.AsyncClient, optional
        Inject a pre-configured client (useful in tests).
    api_url : str, optional
        Override the GED API URL (useful in tests).
    request_timeout : float, optional
        Per-request timeout in seconds.
    max_attempts : int, optional
        Maximum retry attempts per HTTP request.
    pagesize : int, optional
        Records per page (max 1000).
    """

    name: str = "ucdp_ged"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        api_url: str = UCDP_GED_API_URL,
        request_timeout: float = 30.0,
        max_attempts: int = 5,
        pagesize: int = UCDP_MAX_PAGESIZE,
    ) -> None:
        self._api_url = api_url
        self._timeout = request_timeout
        self._max_attempts = max_attempts
        self._pagesize = min(pagesize, UCDP_MAX_PAGESIZE)
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> UCDPConnector:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def query_events(
        self,
        start: date,
        end: date,
        country_ids: tuple[int, ...] | list[int] = DEFAULT_COUNTRY_IDS,
    ) -> AsyncIterator[dict[str, Any]]:
        """Query the UCDP GED API for events in [start, end].

        Parameters
        ----------
        start, end : date
            Inclusive date window.
        country_ids : sequence of int
            UCDP/GW country IDs to filter on.

        Yields
        ------
        dict
            One record per event with ``_source``, ``detected_at``, and
            ``event`` (a ``UCDPEvent`` instance).
        """
        country_set = set(country_ids)
        page = 0

        while True:
            params: dict[str, str] = {
                "pagesize": str(self._pagesize),
                "page": str(page),
                "StartDate": start.isoformat(),
                "EndDate": end.isoformat(),
            }

            raw_body, payload = await self._get_with_retry(self._api_url, params)

            results: list[dict[str, Any]] = payload.get("Result", [])
            total_count = int(payload.get("TotalCount", 0))

            if not results:
                return

            source_record = self._build_source(
                raw_body,
                identifier=f"{self._api_url}?page={page}",
                extra_metadata={
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "page": page,
                    "total_count": total_count,
                    "n_results": len(results),
                },
            )

            for record in results:
                record_country_id = int(record.get("country_id", 0))
                if country_ids and record_country_id not in country_set:
                    continue

                event = _parse_ged_record(record)
                if event is None:
                    continue

                yield {
                    **record,
                    "event": event,
                    "_source": source_record,
                    "detected_at": event.detected_at,
                }

            if (page + 1) * self._pagesize >= total_count:
                return
            page += 1

    async def ingest(
        self,
        start: datetime,
        end: datetime,
        bbox: tuple[float, float, float, float],
    ) -> AsyncIterator[dict[str, Any]]:
        """IngestConnector protocol entrypoint.

        Delegates to query_events, converting datetimes to dates and
        post-filtering by bounding box.
        """
        west, south, east, north = bbox
        async for record in self.query_events(
            start=start.date() if isinstance(start, datetime) else start,
            end=end.date() if isinstance(end, datetime) else end,
        ):
            event: UCDPEvent = record["event"]
            if not (south <= event.latitude <= north and west <= event.longitude <= east):
                continue
            yield record

    def _build_source(
        self,
        raw_body: bytes,
        identifier: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Source:
        metadata: dict[str, Any] = {
            "attribution": UCDP_ATTRIBUTION,
        }
        if extra_metadata:
            metadata.update(extra_metadata)

        return Source(
            source_type=SourceType.UCDP,
            identifier=identifier,
            retrieved_at=datetime.now(tz=UTC),
            retrieved_by="wced.ingest.ucdp",
            content_hash=_content_hash(raw_body),
            metadata=metadata,
        )

    async def _get_with_retry(
        self,
        url: str,
        params: dict[str, str],
    ) -> tuple[bytes, dict[str, Any]]:
        if self._client is None:
            raise RuntimeError(
                "UCDPConnector must be used as an async context manager "
                "or initialised with an explicit httpx.AsyncClient"
            )
        client = self._client

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=10.0),
            retry=retry_if_exception_type(
                (httpx.TransportError, _RetryableStatus)
            ),
            reraise=True,
        ):
            with attempt:
                response = await client.get(url, params=params)
                if response.status_code >= 500:
                    log.warning(
                        "ucdp: %s returned %d, retrying",
                        url,
                        response.status_code,
                    )
                    raise _RetryableStatus(response.status_code)
                if response.status_code >= 400:
                    raise UCDPError(
                        f"UCDP request failed: {response.status_code}"
                        f" {response.text[:200]}"
                    )
                raw_body = response.content
                try:
                    payload: dict[str, Any] = response.json()
                except ValueError as exc:
                    raise UCDPError(
                        f"UCDP response was not JSON: {response.text[:200]}"
                    ) from exc
                return raw_body, payload
        raise RuntimeError("unreachable")  # pragma: no cover


class _RetryableStatus(Exception):
    """Internal marker exception that triggers a tenacity retry on 5xx."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"retryable status {status_code}")
        self.status_code = status_code
