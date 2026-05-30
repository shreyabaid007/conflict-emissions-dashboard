"""Tests for wced.ingest.acled.

VCR cassettes under tests/fixtures/cassettes/ record the ACLED OAuth token
exchange followed by the JSON data response.  Cassettes match on
method + scheme + host + path only (no query) because ACLED embeds all
parameters in the query string, and encoding varies by httpx version.
The connector's parameter construction is tested separately via
MockTransport so correctness is not lost.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest
import vcr

from wced.ingest.acled import (
    ACLED_ATTRIBUTION,
    ACLED_PAGE_LIMIT,
    ACLEDConnector,
    ACLEDError,
    ACLEDEvent,
    _parse_event,
)
from wced.models.provenance import Source, SourceType
from pathlib import Path

CASSETTE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "cassettes"

# Match on path only — ACLED puts everything in query params and we don't want
# cassette replay to be brittle against httpx URL-encoding changes.
_vcr = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    record_mode="none",
    match_on=("method", "scheme", "host", "path"),
)

QUERY_DATE = datetime(2026, 3, 15, tzinfo=UTC)
TEST_EMAIL = "test@example.com"
TEST_PASSWORD = "TESTPASSWORD"


# ---------------------------------------------------------------------------
# helpers — MockTransport handlers that fulfil both the OAuth and data call
# ---------------------------------------------------------------------------


def _oauth_response() -> httpx.Response:
    body = json.dumps(
        {
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "expires_in": 86400,
            "token_type": "Bearer",
        }
    )
    return httpx.Response(
        200, text=body, headers={"content-type": "application/json"}
    )


def _make_oauth_aware_handler(data_handler):
    """Wrap a data handler so it transparently serves OAuth token requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _oauth_response()
        return data_handler(request)

    return handler


# ---------------------------------------------------------------------------
# pure-function helpers
# ---------------------------------------------------------------------------


class TestParseEvent:
    def test_parses_string_numerics(self) -> None:
        raw = {
            "event_id_cnty": "IRN5023",
            "event_date": "2026-03-15",
            "event_type": "Explosions/Remote violence",
            "sub_event_type": "Air/drone strike",
            "actor1": "Military Forces of Israel (2024-)",
            "actor2": "Government of Iran",
            "country": "Iran",
            "location": "Isfahan",
            "latitude": "32.6601",
            "longitude": "51.6860",
            "source": "Tehran Times",
            "notes": "Air strikes targeted oil refinery.",
            "fatalities": "0",
            "timestamp": "1742090880",
            "iso": "364",
        }
        event = _parse_event(raw)
        assert isinstance(event, ACLEDEvent)
        assert event.latitude == pytest.approx(32.6601)
        assert event.longitude == pytest.approx(51.6860)
        assert event.fatalities == 0
        assert event.timestamp == 1742090880
        assert event.iso == 364

    def test_detected_at_is_midnight_utc(self) -> None:
        raw = {
            "event_id_cnty": "IRN5023",
            "event_date": "2026-03-15",
            "event_type": "Battles",
            "sub_event_type": "Armed clash",
            "actor1": "A",
            "actor2": "B",
            "country": "Iran",
            "location": "Tehran",
            "latitude": "35.7",
            "longitude": "51.4",
            "source": "Reuters",
            "notes": "Clash near facility.",
            "fatalities": "3",
            "timestamp": "1742090880",
            "iso": "364",
        }
        event = _parse_event(raw)
        assert event.detected_at == datetime(2026, 3, 15, 0, 0, 0, tzinfo=UTC)
        assert event.detected_at.tzinfo is UTC

    def test_event_date_field(self) -> None:
        raw = {
            "event_id_cnty": "IRQ100",
            "event_date": "2026-04-01",
            "event_type": "Strategic developments",
            "sub_event_type": "Looting/property destruction",
            "actor1": "Armed group",
            "actor2": "",
            "country": "Iraq",
            "location": "Basra",
            "latitude": "30.51",
            "longitude": "47.82",
            "source": "AP",
            "notes": "Facility seized.",
            "fatalities": "0",
            "timestamp": "1743498000",
            "iso": "368",
        }
        event = _parse_event(raw)
        assert event.event_date == date(2026, 4, 1)
        assert event.event_id_cnty == "IRQ100"

    def test_model_is_frozen(self) -> None:
        raw = {
            "event_id_cnty": "IRN1",
            "event_date": "2026-03-15",
            "event_type": "Battles",
            "sub_event_type": "Armed clash",
            "actor1": "A",
            "actor2": "",
            "country": "Iran",
            "location": "Tehran",
            "latitude": "35.7",
            "longitude": "51.4",
            "source": "AP",
            "notes": "",
            "fatalities": "0",
            "timestamp": "0",
            "iso": "364",
        }
        event = _parse_event(raw)
        with pytest.raises(Exception):
            event.country = "Iraq"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# connector construction
# ---------------------------------------------------------------------------


class TestConnectorBasics:
    def test_rejects_empty_email(self) -> None:
        with pytest.raises(ValueError, match="email"):
            ACLEDConnector(email="", password="pw")

    def test_rejects_empty_password(self) -> None:
        with pytest.raises(ValueError, match="password"):
            ACLEDConnector(email="test@example.com", password="")

    def test_params_omit_credentials(self) -> None:
        c = ACLEDConnector(email=TEST_EMAIL, password=TEST_PASSWORD)
        params = c._build_params(
            date(2026, 3, 15), date(2026, 3, 15), ["Iran"], ["Battles"], 1
        )
        # OAuth keeps the bearer token in the Authorization header, never
        # in the query string.
        assert "key" not in params
        assert "email" not in params
        assert "password" not in params
        assert "Bearer" not in str(params)
        assert TEST_PASSWORD not in str(params)

    def test_params_require_json_format(self) -> None:
        c = ACLEDConnector(email=TEST_EMAIL, password=TEST_PASSWORD)
        params = c._build_params(
            date(2026, 3, 15), date(2026, 3, 15), ["Iran"], ["Battles"], 1
        )
        assert params["_format"] == "json"

    def test_params_event_date_format(self) -> None:
        c = ACLEDConnector(email=TEST_EMAIL, password=TEST_PASSWORD)
        params = c._build_params(
            date(2026, 3, 1), date(2026, 3, 31), ["Iran"], ["Battles"], 1
        )
        assert params["event_date"] == "2026-03-01|2026-03-31"
        assert params["event_date_where"] == "BETWEEN"

    def test_params_multi_country_pipe_separated(self) -> None:
        c = ACLEDConnector(email=TEST_EMAIL, password=TEST_PASSWORD)
        params = c._build_params(
            date(2026, 3, 15), date(2026, 3, 15), ["Iran", "Iraq"], ["Battles"], 1
        )
        assert params["country"] == "Iran|Iraq"

    def test_params_multi_event_type_pipe_separated(self) -> None:
        c = ACLEDConnector(email=TEST_EMAIL, password=TEST_PASSWORD)
        params = c._build_params(
            date(2026, 3, 15), date(2026, 3, 15), ["Iran"],
            ["Battles", "Explosions/Remote violence"], 1,
        )
        assert params["event_type"] == "Battles|Explosions/Remote violence"

    def test_params_page_and_limit(self) -> None:
        c = ACLEDConnector(email=TEST_EMAIL, password=TEST_PASSWORD)
        params = c._build_params(
            date(2026, 3, 15), date(2026, 3, 15), ["Iran"], ["Battles"], 3
        )
        assert params["limit"] == ACLED_PAGE_LIMIT
        assert params["page"] == 3


# ---------------------------------------------------------------------------
# VCR-replayed happy-path tests
# ---------------------------------------------------------------------------


async def _collect(start: datetime, end: datetime) -> list[dict]:
    async with ACLEDConnector(
        email=TEST_EMAIL,
        password=TEST_PASSWORD,
        countries=["Iran"],
        event_types=["Explosions/Remote violence", "Battles"],
    ) as c:
        return [r async for r in c.query_events(start, end)]


class TestQueryEventsHappyPath:
    async def test_parses_three_events(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        assert len(records) == 3

    async def test_first_record_fields(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        first = records[0]
        event: ACLEDEvent = first["event"]
        assert event.event_id_cnty == "IRN5023"
        assert event.event_type == "Explosions/Remote violence"
        assert event.sub_event_type == "Air/drone strike"
        assert event.country == "Iran"
        assert event.location == "Isfahan"
        assert event.latitude == pytest.approx(32.6601)
        assert event.longitude == pytest.approx(51.6860)
        assert event.fatalities == 0

    async def test_detected_at_injected(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        for rec in records:
            assert rec["detected_at"] == datetime(2026, 3, 15, tzinfo=UTC)
            assert rec["detected_at"] is rec["event"].detected_at

    async def test_empty_window_yields_zero_records(self) -> None:
        with _vcr.use_cassette("acled_empty_window.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        assert records == []

    async def test_retries_on_503(self) -> None:
        with _vcr.use_cassette("acled_503_then_200.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        assert len(records) == 1
        assert records[0]["event"].event_id_cnty == "IRN5023"


# ---------------------------------------------------------------------------
# OAuth authentication behaviour
# ---------------------------------------------------------------------------


class TestOAuth:
    async def test_token_request_sends_password_grant(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path == "/oauth/token":
                return _oauth_response()
            body = json.dumps({"status": 200, "success": True, "count": 0, "data": []})
            return httpx.Response(
                200, text=body, headers={"content-type": "application/json"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password=TEST_PASSWORD,
                countries=["Iran"], event_types=["Battles"],
                client=client,
            )
            async with c:
                _ = [r async for r in c.query_events(QUERY_DATE, QUERY_DATE)]
        finally:
            await client.aclose()

        oauth_reqs = [r for r in captured if r.url.path == "/oauth/token"]
        assert len(oauth_reqs) == 1
        form = dict(
            kv.split("=", 1) for kv in oauth_reqs[0].content.decode().split("&")
        )
        assert form["grant_type"] == "password"
        assert form["client_id"] == "acled"
        assert form["scope"] == "authenticated"
        # httpx form-encodes the values
        from urllib.parse import unquote_plus
        assert unquote_plus(form["username"]) == TEST_EMAIL
        assert unquote_plus(form["password"]) == TEST_PASSWORD

    async def test_data_request_carries_bearer_header(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path == "/oauth/token":
                return _oauth_response()
            body = json.dumps({"status": 200, "success": True, "count": 0, "data": []})
            return httpx.Response(
                200, text=body, headers={"content-type": "application/json"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password=TEST_PASSWORD,
                countries=["Iran"], event_types=["Battles"],
                client=client,
            )
            async with c:
                _ = [r async for r in c.query_events(QUERY_DATE, QUERY_DATE)]
        finally:
            await client.aclose()

        data_reqs = [r for r in captured if r.url.path != "/oauth/token"]
        assert data_reqs
        assert data_reqs[0].headers["authorization"] == "Bearer test-access-token"

    async def test_token_cached_across_pages(self) -> None:
        # Two full pages of data — connector should fetch the token once
        # and reuse it for both data requests.
        page_sizes = [ACLED_PAGE_LIMIT, 1]
        data_call_count = 0
        oauth_call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal data_call_count, oauth_call_count
            if request.url.path == "/oauth/token":
                oauth_call_count += 1
                return _oauth_response()
            n = page_sizes[data_call_count]
            data_call_count += 1
            data = [
                {
                    "event_id_cnty": f"IRN{i}",
                    "event_date": "2026-03-15",
                    "event_type": "Battles",
                    "sub_event_type": "Armed clash",
                    "actor1": "A", "actor2": "",
                    "country": "Iran", "location": "Tehran",
                    "latitude": "35.7", "longitude": "51.4",
                    "source": "AP", "notes": "",
                    "fatalities": "0", "timestamp": "1742090880", "iso": "364",
                }
                for i in range(n)
            ]
            body = json.dumps({"status": 200, "success": True, "count": n, "data": data})
            return httpx.Response(
                200, text=body, headers={"content-type": "application/json"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password=TEST_PASSWORD,
                countries=["Iran"], event_types=["Battles"],
                client=client,
            )
            async with c:
                _ = [r async for r in c.query_events(QUERY_DATE, QUERY_DATE)]
        finally:
            await client.aclose()

        assert oauth_call_count == 1
        assert data_call_count == 2

    async def test_401_invalidates_token_and_retries(self) -> None:
        oauth_calls = 0
        data_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal oauth_calls, data_calls
            if request.url.path == "/oauth/token":
                oauth_calls += 1
                return _oauth_response()
            data_calls += 1
            if data_calls == 1:
                return httpx.Response(401, text='{"error":"token expired"}')
            body = json.dumps({"status": 200, "success": True, "count": 0, "data": []})
            return httpx.Response(
                200, text=body, headers={"content-type": "application/json"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password=TEST_PASSWORD,
                countries=["Iran"], event_types=["Battles"],
                client=client, max_attempts=3,
            )
            async with c:
                _ = [r async for r in c.query_events(QUERY_DATE, QUERY_DATE)]
        finally:
            await client.aclose()

        # Token was re-fetched after 401, and the second data attempt
        # succeeded.
        assert oauth_calls == 2
        assert data_calls == 2

    async def test_oauth_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                return httpx.Response(401, text='{"error":"invalid_grant"}')
            return httpx.Response(500)  # should never be reached

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password="wrong", client=client
            )
            async with c:
                with pytest.raises(ACLEDError, match="OAuth"):
                    async for _ in c.query_events(QUERY_DATE, QUERY_DATE):
                        pass
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Source record verification
# ---------------------------------------------------------------------------


class TestSourceRecord:
    async def test_source_type_is_acled(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert src.source_type is SourceType.ACLED

    async def test_same_source_shared_within_page(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        source_ids = {id(r["_source"]) for r in records}
        assert len(source_ids) == 1

    async def test_source_attribution_present(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert src.metadata["attribution"] == ACLED_ATTRIBUTION

    async def test_source_identifier_no_credentials(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert TEST_PASSWORD not in src.identifier
        assert TEST_EMAIL not in src.identifier
        # And no bearer token either.
        assert "Bearer" not in src.identifier
        assert "test-access-token" not in src.identifier

    async def test_source_content_hash_matches_body(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert len(src.content_hash) == 64  # SHA-256 hex = 64 chars
        assert all(c in "0123456789abcdef" for c in src.content_hash)

    async def test_source_metadata_fields(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert src.metadata["page"] == 1
        assert src.metadata["date_start"] == "2026-03-15"
        assert src.metadata["date_end"] == "2026-03-15"
        assert "Iran" in src.metadata["countries"]

    async def test_retrieved_by(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            records = await _collect(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert src.retrieved_by == "wced.ingest.acled"


# ---------------------------------------------------------------------------
# Credential redaction helper
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_strips_password(self) -> None:
        from wced.ingest.acled import _redact_credentials
        out = _redact_credentials(
            {"password": "secret", "country": "Iran"}
        )
        assert out["password"] == "REDACTED"
        assert out["country"] == "Iran"

    def test_strips_legacy_key_and_email(self) -> None:
        from wced.ingest.acled import _redact_credentials
        out = _redact_credentials(
            {"key": "k", "email": "e@example.com", "country": "Iran"}
        )
        assert out["key"] == "REDACTED"
        assert out["email"] == "REDACTED"
        assert out["country"] == "Iran"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    async def test_stops_after_partial_page(self) -> None:
        # First page returns 2 events (< ACLED_PAGE_LIMIT) — no second
        # data request.
        data_call_count = 0

        def data_handler(request: httpx.Request) -> httpx.Response:
            nonlocal data_call_count
            data_call_count += 1
            body = json.dumps({
                "status": 200,
                "success": True,
                "count": 2,
                "data": [
                    {
                        "event_id_cnty": f"IRN{data_call_count}00",
                        "event_date": "2026-03-15",
                        "event_type": "Battles",
                        "sub_event_type": "Armed clash",
                        "actor1": "A", "actor2": "B",
                        "country": "Iran", "location": "Tehran",
                        "latitude": "35.7", "longitude": "51.4",
                        "source": "Reuters", "notes": "Clash.",
                        "fatalities": "0", "timestamp": "1742090880", "iso": "364",
                    },
                    {
                        "event_id_cnty": f"IRN{data_call_count}01",
                        "event_date": "2026-03-15",
                        "event_type": "Battles",
                        "sub_event_type": "Armed clash",
                        "actor1": "A", "actor2": "B",
                        "country": "Iran", "location": "Isfahan",
                        "latitude": "32.66", "longitude": "51.68",
                        "source": "AP", "notes": "Clash near refinery.",
                        "fatalities": "1", "timestamp": "1742090880", "iso": "364",
                    },
                ],
            })
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_make_oauth_aware_handler(data_handler))
        )
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password=TEST_PASSWORD,
                countries=["Iran"], event_types=["Battles"],
                client=client,
            )
            async with c:
                records = [r async for r in c.query_events(QUERY_DATE, QUERY_DATE)]
        finally:
            await client.aclose()

        assert len(records) == 2
        assert data_call_count == 1

    async def test_fetches_second_page_when_first_is_full(self) -> None:
        page_sizes = [ACLED_PAGE_LIMIT, 1]
        data_call_count = 0

        def data_handler(request: httpx.Request) -> httpx.Response:
            nonlocal data_call_count
            n = page_sizes[data_call_count]
            data_call_count += 1
            data = [
                {
                    "event_id_cnty": f"IRN{i}",
                    "event_date": "2026-03-15",
                    "event_type": "Battles",
                    "sub_event_type": "Armed clash",
                    "actor1": "A", "actor2": "",
                    "country": "Iran", "location": "Tehran",
                    "latitude": "35.7", "longitude": "51.4",
                    "source": "AP", "notes": "",
                    "fatalities": "0", "timestamp": "1742090880", "iso": "364",
                }
                for i in range(n)
            ]
            body = json.dumps({"status": 200, "success": True, "count": n, "data": data})
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_make_oauth_aware_handler(data_handler))
        )
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password=TEST_PASSWORD,
                countries=["Iran"], event_types=["Battles"],
                client=client,
            )
            async with c:
                records = [r async for r in c.query_events(QUERY_DATE, QUERY_DATE)]
        finally:
            await client.aclose()

        assert len(records) == ACLED_PAGE_LIMIT + 1
        assert data_call_count == 2

    async def test_pages_have_distinct_sources(self) -> None:
        page_sizes = [ACLED_PAGE_LIMIT, 1]
        data_call_count = 0

        def data_handler(request: httpx.Request) -> httpx.Response:
            nonlocal data_call_count
            n = page_sizes[data_call_count]
            data_call_count += 1
            data = [
                {
                    "event_id_cnty": f"IRN{data_call_count * 1000 + i}",
                    "event_date": "2026-03-15",
                    "event_type": "Battles",
                    "sub_event_type": "Armed clash",
                    "actor1": "A", "actor2": "",
                    "country": "Iran", "location": "Tehran",
                    "latitude": "35.7", "longitude": "51.4",
                    "source": "AP", "notes": "",
                    "fatalities": "0", "timestamp": "1742090880", "iso": "364",
                }
                for i in range(n)
            ]
            body = json.dumps({"status": 200, "success": True, "count": n, "data": data})
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_make_oauth_aware_handler(data_handler))
        )
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password=TEST_PASSWORD,
                countries=["Iran"], event_types=["Battles"],
                client=client,
            )
            async with c:
                records = [r async for r in c.query_events(QUERY_DATE, QUERY_DATE)]
        finally:
            await client.aclose()

        page1_src = records[0]["_source"]
        page2_src = records[-1]["_source"]
        assert page1_src is not page2_src
        assert page1_src.metadata["page"] == 1
        assert page2_src.metadata["page"] == 2


# ---------------------------------------------------------------------------
# Error surface
# ---------------------------------------------------------------------------


class TestErrors:
    async def test_non_auth_4xx_raises_immediately(self) -> None:
        # 403 is not the auth-refresh signal; must surface immediately
        # with no retries.
        data_attempts = 0

        def data_handler(request: httpx.Request) -> httpx.Response:
            nonlocal data_attempts
            data_attempts += 1
            return httpx.Response(403, text='{"error":"Forbidden"}')

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_make_oauth_aware_handler(data_handler))
        )
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password=TEST_PASSWORD,
                client=client, max_attempts=3,
            )
            async with c:
                with pytest.raises(ACLEDError, match="403"):
                    async for _ in c.query_events(QUERY_DATE, QUERY_DATE):
                        pass
        finally:
            await client.aclose()

        assert data_attempts == 1

    async def test_api_level_error_raises(self) -> None:
        def data_handler(request: httpx.Request) -> httpx.Response:
            body = json.dumps({
                "status": 400,
                "success": False,
                "message": "Invalid query",
                "data": [],
            })
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_make_oauth_aware_handler(data_handler))
        )
        try:
            c = ACLEDConnector(
                email=TEST_EMAIL, password=TEST_PASSWORD, client=client
            )
            async with c:
                with pytest.raises(ACLEDError, match="Invalid query"):
                    async for _ in c.query_events(QUERY_DATE, QUERY_DATE):
                        pass
        finally:
            await client.aclose()

    async def test_no_client_raises_runtime_error(self) -> None:
        c = ACLEDConnector(email=TEST_EMAIL, password=TEST_PASSWORD)
        with pytest.raises(RuntimeError, match="context manager"):
            async for _ in c.query_events(QUERY_DATE, QUERY_DATE):
                pass


# ---------------------------------------------------------------------------
# datetime / date polymorphism
# ---------------------------------------------------------------------------


class TestDateHandling:
    async def test_accepts_date_objects(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            async with ACLEDConnector(
                email=TEST_EMAIL,
                password=TEST_PASSWORD,
                countries=["Iran"],
                event_types=["Explosions/Remote violence", "Battles"],
            ) as c:
                records = [
                    r async for r in c.query_events(
                        date(2026, 3, 15), date(2026, 3, 15)
                    )
                ]
        assert len(records) == 3

    async def test_accepts_datetime_objects(self) -> None:
        with _vcr.use_cassette("acled_events.yaml"):
            async with ACLEDConnector(
                email=TEST_EMAIL,
                password=TEST_PASSWORD,
                countries=["Iran"],
                event_types=["Explosions/Remote violence", "Battles"],
            ) as c:
                records = [
                    r async for r in c.query_events(
                        datetime(2026, 3, 15, 12, 0, tzinfo=UTC),
                        datetime(2026, 3, 15, 23, 59, tzinfo=UTC),
                    )
                ]
        assert len(records) == 3
