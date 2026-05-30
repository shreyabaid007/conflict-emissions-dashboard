"""Armed Conflict Location & Event Data Project (ACLED) ingest connector.

ACLED tracks political violence and protest events globally, updated weekly.
For WCED, ACLED records serve as a verification source: a strike or explosion
record that spatially and temporally overlaps a satellite-detected fire
upgrades that event's confidence label from REPORTED → VERIFIED (see
``wced.verify``).

API reference
-------------
Endpoint:   https://acleddata.com/api/acled/read
OAuth:      https://acleddata.com/oauth/token
Docs:       https://acleddata.com/api-documentation/getting-started

Authentication
--------------
ACLED uses OAuth 2.0 (Resource Owner Password Credentials grant).  Obtain
an account at https://acleddata.com/register/, then exchange your account
e-mail and password at the ``/oauth/token`` endpoint for a Bearer access
token (24h expiry) and a refresh token.  Every data request must carry
the access token in an ``Authorization: Bearer <token>`` header.

Credentials are read from environment variables ``ACLED_EMAIL`` and
``ACLED_PASSWORD``; never hard-code them.

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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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

ACLED_BASE_URL: Final[str] = "https://acleddata.com/api/acled/read"
ACLED_OAUTH_URL: Final[str] = "https://acleddata.com/oauth/token"

# Public OAuth client_id documented by ACLED for end-user password grants.
ACLED_OAUTH_CLIENT_ID: Final[str] = "acled"
ACLED_OAUTH_SCOPE: Final[str] = "authenticated"

# Default token lifetime when the server omits ``expires_in`` (24h per docs).
_DEFAULT_TOKEN_LIFETIME_SECONDS: Final[int] = 24 * 60 * 60

# Refresh slightly before actual expiry to avoid edge-of-window 401s.
_TOKEN_REFRESH_LEEWAY_SECONDS: Final[int] = 60

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


@dataclass
class _OAuthToken:
    """Cached OAuth bearer credentials for a single account session."""

    access_token: str
    refresh_token: str
    expires_at: datetime  # UTC

    def is_expired(self, *, now: datetime | None = None) -> bool:
        ref = now if now is not None else datetime.now(tz=UTC)
        return ref >= self.expires_at - timedelta(seconds=_TOKEN_REFRESH_LEEWAY_SECONDS)


def _content_hash(body: bytes) -> str:
    """SHA-256 hex digest of a raw response body."""
    return hashlib.sha256(body).hexdigest()


def _parse_event(raw: dict[str, Any]) -> ACLEDEvent:
    """Construct an :class:`ACLEDEvent` from one raw ACLED JSON record.

    ACLED returns numeric fields (latitude, longitude, fatalities, timestamp,
    iso) as JSON strings, not numbers.  We coerce them here so callers never
    need to handle the ambiguity.
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
    """Return a copy of ``params`` with any credential fields masked.

    The new OAuth flow keeps the access token out of query strings entirely,
    so the only credential that could plausibly appear in a captured params
    dict is ``password`` (defensively, e.g. from an OAuth payload that was
    incorrectly passed in).  Mask any legacy ``key``/``email`` fields too so
    historical params logged before the OAuth migration cannot leak.
    """
    redacted = dict(params)
    for sensitive in ("password", "key", "email"):
        if sensitive in redacted:
            redacted[sensitive] = "REDACTED"
    return redacted


class ACLEDConnector:
    """Async ingest connector for the ACLED OAuth API.

    Queries ACLED's armed conflict event database and yields raw
    :class:`ACLEDEvent` records for the specified date window and countries.
    Each page of API results produces a :class:`~wced.models.provenance.Source`
    record attached to every event dict under the ``_source`` key — callers
    must persist this to satisfy provenance requirements.

    The connector exchanges the supplied account e-mail / password for an
    OAuth bearer token (24h lifetime) the first time a data request is made,
    caches the token in memory, and refreshes it via the refresh-token grant
    when it nears expiry.

    Parameters
    ----------
    email : str
        Registered ACLED account e-mail (used as the OAuth ``username``).
    password : str
        ACLED account password (used as the OAuth ``password`` grant value).
    countries : sequence of str, optional
        Country names to query (ACLED uses English names, not ISO codes).
    event_types : sequence of str, optional
        ACLED event-type strings to include.
    client : httpx.AsyncClient, optional
        Inject a pre-configured client (useful in tests).
    base_url : str, optional
        Override the data API base URL (useful in tests).
    oauth_url : str, optional
        Override the OAuth token endpoint (useful in tests).
    request_timeout : float, optional
        Per-request timeout in seconds.
    max_attempts : int, optional
        Maximum retry attempts per HTTP request before propagating the error.
    """

    name: str = "acled"

    def __init__(
        self,
        email: str,
        password: str,
        *,
        countries: tuple[str, ...] | list[str] = DEFAULT_COUNTRIES,
        event_types: tuple[str, ...] | list[str] = RELEVANT_EVENT_TYPES,
        client: httpx.AsyncClient | None = None,
        base_url: str = ACLED_BASE_URL,
        oauth_url: str = ACLED_OAUTH_URL,
        request_timeout: float = 30.0,
        max_attempts: int = 5,
    ) -> None:
        if not email:
            raise ValueError("ACLED email must be a non-empty string")
        if not password:
            raise ValueError("ACLED password must be a non-empty string")
        self._email = email
        self._password = password
        self._countries = list(countries)
        self._event_types = list(event_types)
        self._base_url = base_url.rstrip("/")
        self._oauth_url = oauth_url
        self._timeout = request_timeout
        self._max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None
        self._token: _OAuthToken | None = None

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
        """Yield raw ACLED event dicts for the given date window."""
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
        """IngestConnector protocol entrypoint; delegates to :meth:`query_events`."""
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
        # OAuth credentials travel in the Authorization header, never in the
        # query string.  ``_format=json`` is mandatory per the current docs.
        return {
            "_format": "json",
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
        # Build a credential-free identifier for storage/display.  OAuth keeps
        # the bearer token out of the URL, but we still pass params through
        # the redactor so any future credential field is masked defensively.
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

    # ---- OAuth ----------------------------------------------------------

    async def _ensure_token(self) -> str:
        """Return a valid bearer token, fetching or refreshing as needed."""
        if self._token is None:
            await self._fetch_token()
        elif self._token.is_expired():
            try:
                await self._refresh_token()
            except ACLEDError:
                # Refresh failed (e.g. refresh token also expired) — fall back
                # to a fresh password grant.
                log.warning("acled: refresh token rejected, re-authenticating")
                await self._fetch_token()
        assert self._token is not None  # for type checker
        return self._token.access_token

    async def _fetch_token(self) -> None:
        """Exchange e-mail/password for a new bearer + refresh token pair."""
        payload = await self._post_oauth(
            {
                "grant_type": "password",
                "client_id": ACLED_OAUTH_CLIENT_ID,
                "scope": ACLED_OAUTH_SCOPE,
                "username": self._email,
                "password": self._password,
            }
        )
        self._token = self._token_from_payload(payload)

    async def _refresh_token(self) -> None:
        """Use the cached refresh token to mint a new access token."""
        assert self._token is not None
        if not self._token.refresh_token:
            raise ACLEDError("no refresh token available")
        payload = await self._post_oauth(
            {
                "grant_type": "refresh_token",
                "client_id": ACLED_OAUTH_CLIENT_ID,
                "refresh_token": self._token.refresh_token,
            }
        )
        self._token = self._token_from_payload(payload)

    @staticmethod
    def _token_from_payload(payload: dict[str, Any]) -> _OAuthToken:
        try:
            access = str(payload["access_token"])
        except KeyError as exc:
            raise ACLEDError(
                f"ACLED OAuth response missing access_token: {payload}"
            ) from exc
        refresh = str(payload.get("refresh_token", ""))
        try:
            lifetime = int(payload.get("expires_in", _DEFAULT_TOKEN_LIFETIME_SECONDS))
        except (TypeError, ValueError):
            lifetime = _DEFAULT_TOKEN_LIFETIME_SECONDS
        return _OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=lifetime),
        )

    async def _post_oauth(self, data: dict[str, str]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError(
                "ACLEDConnector must be used as an async context manager "
                "or initialised with an explicit httpx.AsyncClient"
            )
        response = await self._client.post(self._oauth_url, data=data)
        if response.status_code >= 400:
            raise ACLEDError(
                f"ACLED OAuth request failed: {response.status_code}"
                f" {response.text[:200]}"
            )
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ACLEDError(
                f"ACLED OAuth response was not JSON: {response.text[:200]}"
            ) from exc
        return payload

    # ---- Data fetch ------------------------------------------------------

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
        # credentials, invalid params) and must not be retried — except 401,
        # which we treat as a token-expiry signal: refresh once and retry.
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=10.0),
            retry=retry_if_exception_type((httpx.TransportError, _RetryableStatus)),
            reraise=True,
        ):
            with attempt:
                token = await self._ensure_token()
                headers = {"Authorization": f"Bearer {token}"}
                response = await client.get(
                    self._base_url, params=params, headers=headers
                )
                if response.status_code == 401 and self._token is not None:
                    # Server rejected the cached token; force a refresh and
                    # let tenacity retry the request.
                    log.info("acled: 401 from data endpoint, invalidating token")
                    self._token = None
                    raise _RetryableStatus(401)
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
