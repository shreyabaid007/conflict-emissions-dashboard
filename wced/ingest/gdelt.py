"""GDELT 2.0 Events Database ingest connector.

GDELT (Global Database of Events, Language, and Tone) monitors broadcast,
print, and web news worldwide, translating human activities into a
machine-codified event stream updated every 15 minutes. For WCED, GDELT
records serve as a corroboration source when ACLED API access is unavailable,
or as a supplementary signal alongside ACLED.

GDELT is machine-extracted from news text, not human-reviewed. Therefore
GDELT corroboration can never push an event above REPORTED on its own.
See ``wced.verify.confidence`` for the full decision table.

Two access methods are implemented:

1. **DOC API** (preferred) — ``query_events_api`` queries the GDELT DOC 2.0
   API which searches the last 3 months of articles and returns structured
   JSON. No authentication required.

2. **Events 2.0 flat files** (fallback) — ``fetch_latest_events`` downloads
   the most recent 15-minute CSV export from the GDELT Events database,
   filtered by country code and CAMEO event root codes for violent events.

API reference
-------------
DOC API:   https://api.gdeltproject.org/api/v2/doc/doc
Events:    http://data.gdeltproject.org/gdeltv2/lastupdate.txt
Codebook:  http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf

Attribution (mandatory)
-----------------------
GDELT data is fully open. All outputs must carry the following citation:

    Leetaru, K. & Schrodt, P.A. (2013). GDELT: Global Data on Events,
    Location and Tone, 1979-2012. ISA Annual Convention.

The canonical form is stored in :data:`GDELT_ATTRIBUTION`.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any, Final

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wced.models.provenance import ConfidenceLabel, Source, SourceType

log = logging.getLogger(__name__)

GDELT_DOC_API_URL: Final[str] = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
)
GDELT_LASTUPDATE_URL: Final[str] = (
    "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
)

GDELT_ATTRIBUTION: Final[str] = (
    "GDELT Project (gdeltproject.org); Leetaru, K. & Schrodt, P.A. (2013)."
    " GDELT: Global Data on Events, Location and Tone, 1979-2012."
    " ISA Annual Convention."
)

# GDELT corroboration is machine-extracted, not human-reviewed.
# It can never push confidence above REPORTED on its own.
GDELT_MAX_CONFIDENCE: Final[ConfidenceLabel] = ConfidenceLabel.REPORTED

# Default DOC API query targeting conflict theatre.
DEFAULT_DOC_QUERY: Final[str] = (
    "(Iran OR Tehran OR Isfahan OR Bandar Abbas OR refinery OR oil depot)"
    " (strike OR bomb OR attack OR explosion OR fire)"
)

# CAMEO event root codes for violent events.
# 18 = Assault, 19 = Fight, 20 = Use unconventional mass violence.
VIOLENT_ROOT_CODES: Final[frozenset[str]] = frozenset({"18", "19", "20"})

# Country codes for the 2026 conflict theatre (ISO FIPS 10-4 used by GDELT).
DEFAULT_COUNTRY_CODES: Final[tuple[str, ...]] = (
    "IR",   # Iran
    "IS",   # Israel
    "BH",   # Bahrain
    "KW",   # Kuwait
    "QA",   # Qatar
    "AE",   # United Arab Emirates
    "OM",   # Oman
    "SA",   # Saudi Arabia
    "IQ",   # Iraq
)

# Maximum records per DOC API request.
DOC_API_MAX_RECORDS: Final[int] = 250


class GDELTEvent(BaseModel):
    """Raw upstream GDELT event record.

    Field names mirror the GDELT Events 2.0 schema so the connector contains
    no domain-specific mapping. Translating to WCED domain models is the
    responsibility of ``wced.verify``.

    This model is intentionally separate from ``ACLEDEvent``: GDELT uses CAMEO
    codes, has different spatial precision, and includes tone/article-count
    metadata that ACLED does not.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    event_date: date
    event_type: str  # CAMEO EventCode (full)
    event_root_code: str  # First two digits of EventCode
    actor1: str
    actor2: str
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    source_url: str
    num_articles: int = Field(ge=0)
    avg_tone: float
    goldstein_scale: float
    detected_at: AwareDatetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_violent(self) -> bool:
        """True iff EventRootCode indicates assault, fight, or mass violence."""
        return self.event_root_code in VIOLENT_ROOT_CODES


class GDELTError(RuntimeError):
    """Raised when a GDELT API or file fetch returns a non-recoverable error."""


def _content_hash(body: bytes) -> str:
    """SHA-256 hex digest of a raw response body."""
    return hashlib.sha256(body).hexdigest()


def _parse_doc_article(article: dict[str, Any]) -> GDELTEvent | None:
    """Parse a single article from the DOC API JSON response into a GDELTEvent.

    Returns None if required geolocation fields are missing or invalid.
    """
    try:
        lat = float(article.get("sourcecountylat") or article.get("seendate", "0")[:0] or 0)
        lon = float(article.get("sourcecountylon") or 0)
    except (TypeError, ValueError):
        return None

    if lat == 0.0 and lon == 0.0:
        return None

    seen = str(article.get("seendate", ""))
    if len(seen) >= 8:
        try:
            event_date = date(int(seen[:4]), int(seen[4:6]), int(seen[6:8]))
        except ValueError:
            event_date = date.today()
    else:
        event_date = date.today()

    detected_at = datetime(
        event_date.year, event_date.month, event_date.day, tzinfo=UTC
    )

    url = str(article.get("url", ""))
    title = str(article.get("title", ""))
    domain = str(article.get("domain", ""))

    return GDELTEvent(
        event_id=_content_hash(url.encode())[:16],
        event_date=event_date,
        event_type="",
        event_root_code="",
        actor1="",
        actor2="",
        latitude=lat,
        longitude=lon,
        source_url=url,
        num_articles=int(article.get("sharingimage_maxlinks", 1)),
        avg_tone=float(article.get("tone", 0.0)),
        goldstein_scale=0.0,
        detected_at=detected_at,
    )


def _parse_csv_row(row: dict[str, str]) -> GDELTEvent | None:
    """Parse a single row from a GDELT Events 2.0 CSV export.

    GDELT CSV columns are tab-separated with numbered headers per the
    codebook. We use the named columns from the header row.

    Returns None if required fields are missing or the event is outside
    the violent-event filter.
    """
    try:
        event_root_code = str(row.get("EventRootCode", ""))
        lat = float(row.get("ActionGeo_Lat", 0))
        lon = float(row.get("ActionGeo_Long", 0))
    except (TypeError, ValueError):
        return None

    if lat == 0.0 and lon == 0.0:
        return None

    date_str = str(row.get("SQLDATE", row.get("Day", "")))
    if len(date_str) >= 8:
        try:
            event_date = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
        except ValueError:
            return None
    else:
        return None

    detected_at = datetime(
        event_date.year, event_date.month, event_date.day, tzinfo=UTC
    )

    return GDELTEvent(
        event_id=str(row.get("GLOBALEVENTID", "")),
        event_date=event_date,
        event_type=str(row.get("EventCode", "")),
        event_root_code=event_root_code,
        actor1=str(row.get("Actor1Name", "")),
        actor2=str(row.get("Actor2Name", "")),
        latitude=lat,
        longitude=lon,
        source_url=str(row.get("SOURCEURL", "")),
        num_articles=int(row.get("NumArticles", 1)),
        avg_tone=float(row.get("AvgTone", 0.0)),
        goldstein_scale=float(row.get("GoldsteinScale", 0.0)),
        detected_at=detected_at,
    )


class GDELTConnector:
    """Async ingest connector for GDELT 2.0.

    Provides two methods for fetching conflict event data:

    1. ``query_events_api`` — queries the GDELT DOC 2.0 API (preferred;
       searches last 3 months of articles, returns JSON, no auth required).
    2. ``fetch_latest_events`` — downloads the latest 15-minute CSV export
       from GDELT Events 2.0 flat files and filters by country + CAMEO codes.

    Both methods yield dicts with ``_source`` (provenance) and ``detected_at``.

    No authentication is required for either method.

    Parameters
    ----------
    client : httpx.AsyncClient, optional
        Inject a pre-configured client (useful in tests).
    doc_api_url : str, optional
        Override the DOC API URL (useful in tests).
    request_timeout : float, optional
        Per-request timeout in seconds.
    max_attempts : int, optional
        Maximum retry attempts per HTTP request before propagating the error.
    """

    name: str = "gdelt"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        doc_api_url: str = GDELT_DOC_API_URL,
        request_timeout: float = 30.0,
        max_attempts: int = 5,
    ) -> None:
        self._doc_api_url = doc_api_url
        self._timeout = request_timeout
        self._max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> GDELTConnector:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ public

    async def query_events_api(
        self,
        query: str = DEFAULT_DOC_QUERY,
        start: datetime | date | None = None,
        end: datetime | date | None = None,
        max_records: int = DOC_API_MAX_RECORDS,
    ) -> AsyncIterator[dict[str, Any]]:
        """Query the GDELT DOC 2.0 API for articles matching *query*.

        Parameters
        ----------
        query : str
            Boolean search query (GDELT query syntax).
        start, end : datetime or date, optional
            Time window. Formatted as YYYYMMDDHHMMSS for the API.
        max_records : int
            Maximum records to return (API cap: 250).

        Yields
        ------
        dict
            One record per article with ``_source``, ``detected_at``, and
            ``event`` (a ``GDELTEvent`` instance).
        """
        params: dict[str, str] = {
            "query": query,
            "mode": "ArtList",
            "maxrecords": str(min(max_records, DOC_API_MAX_RECORDS)),
            "format": "json",
        }
        if start is not None:
            params["startdatetime"] = self._format_datetime(start)
        if end is not None:
            params["enddatetime"] = self._format_datetime(end)

        raw_body, payload = await self._get_with_retry(
            self._doc_api_url, params
        )

        articles: list[dict[str, Any]] = payload.get("articles", [])
        if not articles:
            return

        source_record = self._build_source(
            raw_body,
            identifier=f"{self._doc_api_url}?query={query}",
            extra_metadata={
                "query": query,
                "start": params.get("startdatetime"),
                "end": params.get("enddatetime"),
                "n_articles": len(articles),
            },
        )

        for article in articles:
            event = _parse_doc_article(article)
            if event is None:
                continue
            yield {
                **article,
                "event": event,
                "_source": source_record,
                "detected_at": event.detected_at,
            }

    async def fetch_latest_events(
        self,
        country_codes: tuple[str, ...] | list[str] = DEFAULT_COUNTRY_CODES,
    ) -> AsyncIterator[dict[str, Any]]:
        """Download the latest 15-minute GDELT Events CSV and filter.

        Fetches ``lastupdate.txt`` to discover the current export URL,
        downloads the CSV, and yields rows where
        ``ActionGeo_CountryCode in country_codes`` and
        ``EventRootCode in VIOLENT_ROOT_CODES``.

        Parameters
        ----------
        country_codes : sequence of str
            FIPS 10-4 country codes to include.

        Yields
        ------
        dict
            One record per matching event row with ``_source``,
            ``detected_at``, and ``event`` (a ``GDELTEvent`` instance).
        """
        country_set = set(country_codes)

        # Step 1: get the latest export URL from lastupdate.txt
        raw_update, _ = await self._get_with_retry(
            GDELT_LASTUPDATE_URL, {}, expect_json=False,
        )
        csv_url = self._extract_export_url(raw_update.decode("utf-8", errors="replace"))
        if csv_url is None:
            log.warning("gdelt: could not extract CSV URL from lastupdate.txt")
            return

        # Step 2: download the CSV
        raw_csv, _ = await self._get_with_retry(
            csv_url, {}, expect_json=False,
        )

        source_record = self._build_source(
            raw_csv,
            identifier=csv_url,
            extra_metadata={
                "source": "gdelt_events_2.0_csv",
                "country_codes": list(country_codes),
            },
        )

        # Step 3: parse and filter
        text = raw_csv.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        for row in reader:
            geo_country = row.get("ActionGeo_CountryCode", "")
            event_root = row.get("EventRootCode", "")

            if geo_country not in country_set:
                continue
            if event_root not in VIOLENT_ROOT_CODES:
                continue

            event = _parse_csv_row(row)
            if event is None:
                continue

            yield {
                **row,
                "event": event,
                "_source": source_record,
                "detected_at": event.detected_at,
            }

    async def ingest(
        self,
        start: datetime,
        end: datetime,
        bbox: tuple[float, float, float, float],
    ) -> AsyncIterator[dict[str, Any]]:
        """IngestConnector protocol entrypoint; delegates to query_events_api."""
        async for record in self.query_events_api(
            query=DEFAULT_DOC_QUERY, start=start, end=end,
        ):
            yield record

    # ------------------------------------------------------------------ internal

    @staticmethod
    def _format_datetime(dt: datetime | date) -> str:
        """Format a datetime/date as YYYYMMDDHHMMSS for the DOC API."""
        if isinstance(dt, datetime):
            return dt.strftime("%Y%m%d%H%M%S")
        return f"{dt.strftime('%Y%m%d')}000000"

    @staticmethod
    def _extract_export_url(lastupdate_text: str) -> str | None:
        """Extract the events export CSV URL from lastupdate.txt.

        lastupdate.txt contains lines like:
          <size> <hash> <url>
        The export URL ends in .export.CSV.zip; we want the unzipped CSV
        URL which is the same but without .zip, or the first URL listed.
        """
        for line in lastupdate_text.strip().splitlines():
            parts = line.strip().split()
            if len(parts) >= 3:
                url = parts[-1]
                if "export" in url.lower():
                    return url
        return None

    def _build_source(
        self,
        raw_body: bytes,
        identifier: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Source:
        metadata: dict[str, Any] = {
            "attribution": GDELT_ATTRIBUTION,
        }
        if extra_metadata:
            metadata.update(extra_metadata)

        return Source(
            source_type=SourceType.GDELT,
            identifier=identifier,
            retrieved_at=datetime.now(tz=UTC),
            retrieved_by="wced.ingest.gdelt",
            content_hash=_content_hash(raw_body),
            metadata=metadata,
        )

    async def _get_with_retry(
        self,
        url: str,
        params: dict[str, str],
        *,
        expect_json: bool = True,
    ) -> tuple[bytes, dict[str, Any]]:
        if self._client is None:
            raise RuntimeError(
                "GDELTConnector must be used as an async context manager "
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
                        "gdelt: %s returned %d, retrying",
                        url,
                        response.status_code,
                    )
                    raise _RetryableStatus(response.status_code)
                if response.status_code >= 400:
                    raise GDELTError(
                        f"GDELT request failed: {response.status_code}"
                        f" {response.text[:200]}"
                    )
                raw_body = response.content
                if expect_json:
                    try:
                        payload: dict[str, Any] = response.json()
                    except ValueError as exc:
                        raise GDELTError(
                            f"GDELT response was not JSON: {response.text[:200]}"
                        ) from exc
                else:
                    payload = {}
                return raw_body, payload
        raise RuntimeError("unreachable")  # pragma: no cover


class _RetryableStatus(Exception):
    """Internal marker exception that triggers a tenacity retry on 5xx."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"retryable status {status_code}")
        self.status_code = status_code
