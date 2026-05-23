"""Armed Conflict Location & Event Data Project (ACLED) ingest connector.

ACLED tracks political violence and protest events globally, updated weekly.
For WCED, ACLED records serve as a verification source: a strike or explosion
record that spatially and temporally overlaps a satellite-detected fire
upgrades that event's confidence label from REPORTED → VERIFIED (see
``wced.verify``).

API reference
-------------
Endpoint:   https://api.acleddata.com/acled/read
Docs:       https://apidocs.acleddata.com/
Version:    ACLED API v2

Authentication
--------------
Every request must include ``email`` and ``key`` query parameters.  Obtain
credentials at https://acleddata.com/register/.  The free academic tier gives
full historical data with a ~1-week update lag.  Credentials are read from
environment variables; never hard-code them.

Attribution requirement (mandatory)
-------------------------------------
ACLED data is published under Creative Commons Attribution-NonCommercial 4.0
International (CC BY-NC 4.0).  ANY output — display, export, derived
computation — that includes or is informed by ACLED data **must** carry the
following attribution string verbatim:

    "ACLED (https://acleddata.com); Armed Conflict Location & Event Data
    Project; acleddata.com"

The canonical form is stored in :data:`ACLED_ATTRIBUTION`.  Every
:class:`~wced.models.provenance.Source` record produced by
:class:`ACLEDConnector` embeds this string in
``metadata["attribution"]`` so downstream presenters can surface it without
re-implementing the requirement.

Event types queried
-------------------
WCED restricts queries to event types that plausibly co-locate with
infrastructure strikes or destruction events relevant to fire/emissions:

- ``Explosions/Remote violence`` — air/drone strikes, missile attacks, IEDs
- ``Battles`` — armed clashes near industrial or infrastructure sites
- ``Strategic developments`` — facility seizure, supply-route disruptions

Other ACLED types (Protests, Riots, Violence against civilians) are excluded
as they rarely co-locate with oil-and-gas infrastructure fires.

Pagination
----------
The API returns at most ``limit`` rows per page (hard cap 500).  The
connector paginates automatically; each page produces its own
:class:`~wced.models.provenance.Source` record because the HTTP response
body differs per page and hashing ensures bit-perfect provenance.
"""
from __future__ import annotations

import enum
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

ACLED_BASE_URL: Final[str] = "https://api.acleddata.com/acled/read"

# ACLED's hard page-size cap. The connector requests this many rows per page
# and uses a shorter response as the "last page" sentinel.
ACLED_PAGE_LIMIT: Final[int] = 500

ACLED_ATTRIBUTION: Final[str] = (
    "ACLED (https://acleddata.com); Armed Conflict Location & Event Data"
    " Project; acleddata.com"
)

# Event types relevant to infrastructure strike / fire attribution.
RELEVANT_EVENT_TYPES: Final[tuple[str, ...]] = (
    "Explosions/Remote violence",
    "Battles",
    "Strategic developments",
)

# Default country scope covering the 2026 conflict theatre.
DEFAULT_COUNTRIES: Final[tuple[str, ...]] = (
    "Iran",
    "Israel",
    "Bahrain",
    "Kuwait",
    "Qatar",
    "United Arab Emirates",
    "Oman",
    "Saudi Arabia",
    "Iraq",
)


class ACLEDEventType(str, enum.Enum):
    """ACLED top-level event type classification."""

    EXPLOSIONS_REMOTE_VIOLENCE = "Explosions/Remote violence"
    BATTLES = "Battles"
    STRATEGIC_DEVELOPMENTS = "Strategic developments"
    PROTESTS = "Protests"
    RIOTS = "Riots"
    VIOLENCE_AGAINST_CIVILIANS = "Violence against civilians"


class ACLEDEvent(BaseModel):
    """Raw upstream ACLED event record.

    Field names mirror the ACLED API JSON response so the connector contains
    no domain-specific mapping.  Translating to WCED domain models (confidence
    label assignment, facility matching) is the responsibility of
    ``wced.verify``.

    This model is intentionally separate from ``wced.models.event.FireEvent``:
    a FireEvent is a WCED domain object anchored to a Facility with quantified
    FRP; an ACLEDEvent is a raw upstream record that *may* corroborate a
    FireEvent after spatial/temporal matching.

    Parameters
    ----------
    event_id_cnty : str
        ACLED composite event identifier (numeric ID + ISO-2 country suffix,
        e.g. ``"IRN5023"``).
    event_date : date
        Date the event was reported (UTC date; ACLED does not record sub-day
        event times).
    event_type : str
        Top-level ACLED category (e.g. ``"Explosions/Remote violence"``).
    sub_event_type : str
        ACLED sub-event classification (e.g. ``"Air/drone strike"``).
    actor1 : str
        Primary actor named in the event record.
    actor2 : str
        Secondary actor (empty string when not applicable).
    country : str
        Country where the event occurred (ACLED English name).
    location : str
        Locality name (city, district, or POI).
    latitude : float
        Centroid latitude in WGS84 (EPSG:4326).
    longitude : float
        Centroid longitude in WGS84 (EPSG:4326).
    source : str
        Primary source cited by ACLED for this record.
    notes : str
        Free-text event description.
    fatalities : int
        ACLED fatality estimate (0 when unknown or not applicable; WCED does
        not store or display casualty figures, but preserves the field for
        completeness).
    timestamp : int
        Unix timestamp (seconds UTC) of when ACLED catalogued this event —
        not the event time itself.
    iso : int
        ISO 3166-1 numeric country code.
    detected_at : AwareDatetime
        UTC datetime derived from ``event_date`` at midnight UTC; added by
        the connector so downstream code can treat all ingest records
        uniformly regardless of source.
    """

    model_config = ConfigDict(frozen=True)

    event_id_cnty: str
    event_date: date
    event_type: str
    sub_event_type: str
    actor1: str
    actor2: str
    country: str
    location: str
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    source: str
    notes: str
    fatalities: int = Field(ge=0)
    timestamp: int
    iso: int
    detected_at: AwareDatetime


class ACLEDError(RuntimeError):
    """Raised when the ACLED API returns a non-recoverable error response."""


def _content_hash(body: bytes) -> str:
    """SHA-256 hex digest of a raw response body."""
    return hashlib.sha256(body).hexdigest()


def _parse_event(raw: dict[str, Any]) -> ACLEDEvent:
    """Construct an :class:`ACLEDEvent` from one raw ACLED JSON record.

    ACLED returns numeric fields (latitude, longitude, fatalities, timestamp,
    iso) as JSON strings, not numbers.  We coerce them here so callers never
    need to handle the ambiguity.

    ``detected_at`` is synthesised as midnight UTC on ``event_date`` because
    ACLED does not record sub-day event times; the ``timestamp`` field reflects
    cataloguing time, not event time.
    """
    event_date = date.fromisoformat(str(raw["event_date"]))
    detected_at = datetime(event_date.year, event_date.month, event_date.day, tzinfo=UTC)
    return ACLEDEvent(
        event_id_cnty=str(raw["event_id_cnty"]),
        event_date=event_date,
        event_type=str(raw.get("event_type", "")),
        sub_event_type=str(raw.get("sub_event_type", "")),
        actor1=str(raw.get("actor1", "")),
        actor2=str(raw.get("actor2", "")),
        country=str(raw.get("country", "")),
        location=str(raw.get("location", "")),
        latitude=float(raw["latitude"]),
        longitude=float(raw["longitude"]),
        source=str(raw.get("source", "")),
        notes=str(raw.get("notes", "")),
        fatalities=int(raw.get("fatalities", 0)),
        timestamp=int(raw.get("timestamp", 0)),
        iso=int(raw.get("iso", 0)),
        detected_at=detected_at,
    )


def _redact_credentials(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``params`` with ``key`` and ``email`` replaced.

    The sanitised copy is used to build a credential-free Source identifier
    suitable for storage and display.
    """
    redacted = dict(params)
    redacted["key"] = "REDACTED"
    redacted["email"] = "REDACTED"
    return redacted


class ACLEDConnector:
    """Async ingest connector for the ACLED API v2.

    Queries ACLED's armed conflict event database and yields raw
    :class:`ACLEDEvent` records for the specified date window and countries.
    Each page of API results produces a :class:`~wced.models.provenance.Source`
    record attached to every event dict under the ``_source`` key — callers
    must persist this to satisfy provenance requirements.

    The ``_source.metadata["attribution"]`` field always contains
    :data:`ACLED_ATTRIBUTION`; any output surface that presents ACLED data
    must propagate this string verbatim to comply with CC BY-NC 4.0.

    Parameters
    ----------
    email : str
        Registered ACLED account e-mail.
    api_key : str
        ACLED API key associated with that account.
    countries : sequence of str, optional
        Country names to query (ACLED uses English names, not ISO codes).
        Defaults to :data:`DEFAULT_COUNTRIES`.
    event_types : sequence of str, optional
        ACLED event-type strings to include.  Defaults to
        :data:`RELEVANT_EVENT_TYPES`.
    client : httpx.AsyncClient, optional
        Inject a pre-configured client (useful in tests).  When omitted the
        connector creates and owns its own client via the async context manager.
    base_url : str, optional
        Override the API base URL (useful in tests).
    request_timeout : float, optional
        Per-request timeout in seconds.
    max_attempts : int, optional
        Maximum retry attempts per HTTP request before propagating the error.
    """

    name: str = "acled"

    def __init__(
        self,
        email: str,
        api_key: str,
        *,
        countries: tuple[str, ...] | list[str] = DEFAULT_COUNTRIES,
        event_types: tuple[str, ...] | list[str] = RELEVANT_EVENT_TYPES,
        client: httpx.AsyncClient | None = None,
        base_url: str = ACLED_BASE_URL,
        request_timeout: float = 30.0,
        max_attempts: int = 5,
    ) -> None:
        if not email:
            raise ValueError("ACLED email must be a non-empty string")
        if not api_key:
            raise ValueError("ACLED api_key must be a non-empty string")
        self._email = email
        self._api_key = api_key
        self._countries = list(countries)
        self._event_types = list(event_types)
        self._base_url = base_url.rstrip("/")
        self._timeout = request_timeout
        self._max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> ACLEDConnector:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ public

    async def query_events(
        self,
        start: date | datetime,
        end: date | datetime,
        countries: list[str] | None = None,
        event_types: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield raw ACLED event dicts for the given date window.

        Each yielded dict contains:

        - All raw API fields from the ACLED response (string-typed as returned)
        - ``event`` (:class:`ACLEDEvent`) — the parsed, type-coerced model
        - ``_source`` (:class:`~wced.models.provenance.Source`) — provenance
          record for the API page that produced this event; the same object
          is shared by all events from a single page fetch
        - ``detected_at`` (:class:`datetime`) — UTC midnight on
          ``event_date`` (ACLED does not record sub-day event times)

        The ``_source`` carries ``metadata["attribution"]`` equal to
        :data:`ACLED_ATTRIBUTION`; any output surface must propagate this.

        Parameters
        ----------
        start : date or datetime
            Inclusive start of the query window.  If a ``datetime`` is
            passed its date component is used.
        end : date or datetime
            Inclusive end of the query window.
        countries : list of str, optional
            Override the instance-level country list for this query only.
        event_types : list of str, optional
            Override the instance-level event-type list for this query only.

        Yields
        ------
        dict[str, Any]
            One dict per ACLED event; see above for the guaranteed keys.
        """
        _countries = countries if countries is not None else self._countries
        _event_types = event_types if event_types is not None else self._event_types
        _start = start.date() if isinstance(start, datetime) else start
        _end = end.date() if isinstance(end, datetime) else end

        page = 1
        while True:
            raw_body, payload = await self._fetch_page(
                _start, _end, _countries, _event_types, page
            )
            data: list[dict[str, Any]] = payload.get("data") or []

            if data:
                source_record = self._build_source(
                    raw_body, _start, _end, _countries, _event_types, page,
                    total_count=int(payload.get("count", 0)),
                )
                for raw in data:
                    event = _parse_event(raw)
                    record: dict[str, Any] = {
                        **raw,
                        "event": event,
                        "_source": source_record,
                        "detected_at": event.detected_at,
                    }
                    yield record

            if len(data) < ACLED_PAGE_LIMIT:
                break
            page += 1

    async def ingest(
        self,
        start: datetime,
        end: datetime,
        bbox: tuple[float, float, float, float],
    ) -> AsyncIterator[dict[str, Any]]:
        """IngestConnector protocol entrypoint; delegates to :meth:`query_events`.

        The ``bbox`` parameter is accepted for protocol compatibility but ACLED
        filters by country name, not bounding box.  Spatial filtering against
        the bbox is the caller's responsibility.
        """
        async for record in self.query_events(start, end):
            yield record

    # ------------------------------------------------------------------ internal

    def _build_params(
        self,
        start: date,
        end: date,
        countries: list[str],
        event_types: list[str],
        page: int,
    ) -> dict[str, Any]:
        return {
            "key": self._api_key,
            "email": self._email,
            "event_date": f"{start.isoformat()}|{end.isoformat()}",
            "event_date_where": "BETWEEN",
            "country": "|".join(countries),
            "event_type": "|".join(event_types),
            "limit": ACLED_PAGE_LIMIT,
            "page": page,
        }

    def _build_source(
        self,
        raw_body: bytes,
        start: date,
        end: date,
        countries: list[str],
        event_types: list[str],
        page: int,
        *,
        total_count: int,
    ) -> Source:
        # Build a credential-free identifier for storage/display.
        safe_params = _redact_credentials(
            self._build_params(start, end, countries, event_types, page)
        )
        qs = "&".join(f"{k}={v}" for k, v in safe_params.items())
        identifier = f"{self._base_url}?{qs}"
        return Source(
            source_type=SourceType.ACLED,
            identifier=identifier,
            retrieved_at=datetime.now(tz=UTC),
            retrieved_by="wced.ingest.acled",
            content_hash=_content_hash(raw_body),
            metadata={
                "attribution": ACLED_ATTRIBUTION,
                "page": page,
                "countries": countries,
                "event_types": event_types,
                "date_start": start.isoformat(),
                "date_end": end.isoformat(),
                "total_count": total_count,
            },
        )

    async def _fetch_page(
        self,
        start: date,
        end: date,
        countries: list[str],
        event_types: list[str],
        page: int,
    ) -> tuple[bytes, dict[str, Any]]:
        params = self._build_params(start, end, countries, event_types, page)
        raw_body, payload = await self._get_with_retry(params)
        return raw_body, payload

    async def _get_with_retry(
        self, params: dict[str, Any]
    ) -> tuple[bytes, dict[str, Any]]:
        if self._client is None:
            raise RuntimeError(
                "ACLEDConnector must be used as an async context manager "
                "or initialised with an explicit httpx.AsyncClient"
            )
        client = self._client

        # Retry on transient transport errors and 5xx.  4xx is fatal (bad
        # credentials, invalid params) and must not be retried.
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=10.0),
            retry=retry_if_exception_type((httpx.TransportError, _RetryableStatus)),
            reraise=True,
        ):
            with attempt:
                response = await client.get(self._base_url, params=params)
                if response.status_code >= 500:
                    log.warning(
                        "acled: %s returned %d, retrying",
                        self._base_url,
                        response.status_code,
                    )
                    raise _RetryableStatus(response.status_code)
                if response.status_code >= 400:
                    raise ACLEDError(
                        f"ACLED request failed: {response.status_code}"
                        f" {response.text[:200]}"
                    )
                raw_body = response.content
                payload: dict[str, Any] = response.json()
                # ACLED returns {"status": 200, "success": true, "data": [...]}
                # or {"status": 400, "success": false, "message": "..."}.
                if not payload.get("success", True):
                    raise ACLEDError(
                        f"ACLED API error: {payload.get('message', payload)}"
                    )
                return raw_body, payload
        raise RuntimeError("unreachable")  # pragma: no cover


class _RetryableStatus(Exception):
    """Internal marker exception that triggers a tenacity retry on 5xx."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"retryable status {status_code}")
        self.status_code = status_code
