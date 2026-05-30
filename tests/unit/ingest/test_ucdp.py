"""Tests for wced.ingest.ucdp.

Uses httpx.MockTransport to simulate the UCDP GED API without network calls.
UCDP is a validation-only source — these tests verify parsing, pagination,
provenance, and error handling.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from wced.ingest.ucdp import (
    DEFAULT_COUNTRY_IDS,
    UCDP_ATTRIBUTION,
    UCDP_MAX_PAGESIZE,
    UCDPConnector,
    UCDPError,
    UCDPEvent,
    _content_hash,
    _parse_ged_record,
)
from wced.models.provenance import Source, SourceType


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_SAMPLE_RECORD: dict = {
    "id": "123456",
    "date_start": "2026-03-15",
    "date_end": "2026-03-15",
    "type_of_violence": 1,
    "conflict_name": "Iran: Government",
    "side_a": "Government of Iran",
    "side_b": "IS",
    "latitude": 32.6601,
    "longitude": 51.686,
    "country": "Iran",
    "country_id": 630,
    "region": "Middle East",
    "source_article": "Reuters 2026-03-15; AP 2026-03-15",
    "best": 3,
    "high": 5,
    "low": 1,
    "where_prec": 1,
    "date_prec": 1,
}

_SAMPLE_RECORD_2: dict = {
    **_SAMPLE_RECORD,
    "id": "123457",
    "latitude": 35.6892,
    "longitude": 51.389,
    "country_id": 630,
    "conflict_name": "Iran: Government (Tehran)",
}

_SAMPLE_RECORD_ISR: dict = {
    **_SAMPLE_RECORD,
    "id": "123458",
    "latitude": 31.7683,
    "longitude": 35.2137,
    "country": "Israel",
    "country_id": 666,
    "conflict_name": "Israel: Government",
}


def _make_api_response(
    results: list[dict],
    total_count: int | None = None,
) -> dict:
    if total_count is None:
        total_count = len(results)
    return {
        "TotalCount": total_count,
        "Result": results,
    }


# ---------------------------------------------------------------------------
# UCDPEvent model tests
# ---------------------------------------------------------------------------


class TestUCDPEvent:
    def test_basic_construction(self) -> None:
        event = UCDPEvent(
            event_id="123456",
            date_start=date(2026, 3, 15),
            date_end=date(2026, 3, 15),
            type_of_violence=1,
            conflict_name="Iran: Government",
            side_a="Gov",
            side_b="IS",
            latitude=32.6601,
            longitude=51.686,
            country="Iran",
            country_id=630,
            region="Middle East",
            source_article="Reuters",
            best_est=3,
            high_est=5,
            low_est=1,
            where_prec=1,
            date_prec=1,
            detected_at=datetime(2026, 3, 15, tzinfo=UTC),
        )
        assert event.event_id == "123456"
        assert event.latitude == pytest.approx(32.6601)
        assert event.best_est == 3

    def test_model_is_frozen(self) -> None:
        event = UCDPEvent(
            event_id="1",
            date_start=date(2026, 3, 15),
            date_end=date(2026, 3, 15),
            type_of_violence=1,
            conflict_name="test",
            side_a="A",
            side_b="B",
            latitude=32.0,
            longitude=51.0,
            country="Iran",
            country_id=630,
            region="Middle East",
            source_article="src",
            best_est=0,
            high_est=0,
            low_est=0,
            where_prec=1,
            date_prec=1,
            detected_at=datetime(2026, 3, 15, tzinfo=UTC),
        )
        with pytest.raises(Exception):
            event.country = "Iraq"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Record parsing
# ---------------------------------------------------------------------------


class TestParseGedRecord:
    def test_parses_valid_record(self) -> None:
        event = _parse_ged_record(_SAMPLE_RECORD)
        assert event is not None
        assert event.event_id == "123456"
        assert event.date_start == date(2026, 3, 15)
        assert event.latitude == pytest.approx(32.6601)
        assert event.longitude == pytest.approx(51.686)
        assert event.best_est == 3
        assert event.type_of_violence == 1
        assert event.where_prec == 1

    def test_rejects_zero_coordinates(self) -> None:
        record = {**_SAMPLE_RECORD, "latitude": 0, "longitude": 0}
        assert _parse_ged_record(record) is None

    def test_rejects_invalid_date(self) -> None:
        record = {**_SAMPLE_RECORD, "date_start": "bad-date"}
        assert _parse_ged_record(record) is None

    def test_rejects_missing_geo(self) -> None:
        record = {k: v for k, v in _SAMPLE_RECORD.items() if k != "latitude"}
        record["latitude"] = None
        assert _parse_ged_record(record) is None

    def test_detected_at_is_midnight_utc(self) -> None:
        event = _parse_ged_record(_SAMPLE_RECORD)
        assert event is not None
        assert event.detected_at == datetime(2026, 3, 15, 0, 0, 0, tzinfo=UTC)
        assert event.detected_at.tzinfo is UTC


# ---------------------------------------------------------------------------
# Connector — mocked HTTP tests
# ---------------------------------------------------------------------------


def _mock_handler_single_page(request: httpx.Request) -> httpx.Response:
    body = _make_api_response([_SAMPLE_RECORD, _SAMPLE_RECORD_2])
    return httpx.Response(200, json=body)


def _mock_handler_empty(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_make_api_response([]))


def _mock_handler_country_filter(request: httpx.Request) -> httpx.Response:
    body = _make_api_response(
        [_SAMPLE_RECORD, _SAMPLE_RECORD_ISR],
    )
    return httpx.Response(200, json=body)


class _PaginationHandler:
    """Returns two pages of results, one record each."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "0"))
        if page == 0:
            body = _make_api_response([_SAMPLE_RECORD], total_count=2)
        else:
            body = _make_api_response([_SAMPLE_RECORD_2], total_count=2)
        self.call_count += 1
        return httpx.Response(200, json=body)


class TestConnectorQueryEvents:
    async def test_parses_two_records(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_single_page),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        assert len(records) == 2

    async def test_first_record_fields(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_single_page),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        event: UCDPEvent = records[0]["event"]
        assert event.event_id == "123456"
        assert event.latitude == pytest.approx(32.6601)

    async def test_detected_at_injected(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_single_page),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        for rec in records:
            assert rec["detected_at"] == datetime(2026, 3, 15, tzinfo=UTC)

    async def test_empty_response_yields_zero(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_empty),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        assert records == []

    async def test_paginates_across_two_pages(self) -> None:
        handler = _PaginationHandler()
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        )
        try:
            async with UCDPConnector(client=client, pagesize=1) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        assert len(records) == 2
        assert handler.call_count == 2

    async def test_filters_by_country_id(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_country_filter),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                iran_only = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                        country_ids=(630,),
                    )
                ]
        finally:
            await client.aclose()
        assert len(iran_only) == 1
        assert iran_only[0]["event"].country == "Iran"


# ---------------------------------------------------------------------------
# IngestConnector protocol — bbox filtering
# ---------------------------------------------------------------------------


class TestIngestProtocol:
    async def test_bbox_filters_records(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_single_page),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                start = datetime(2026, 3, 1, tzinfo=UTC)
                end = datetime(2026, 3, 31, tzinfo=UTC)
                # bbox covering only Isfahan (~32.66, 51.68), not Tehran (~35.69, 51.39)
                records = [
                    r async for r in conn.ingest(
                        start, end, bbox=(51.0, 32.0, 52.0, 33.0),
                    )
                ]
        finally:
            await client.aclose()
        assert len(records) == 1
        assert records[0]["event"].event_id == "123456"


# ---------------------------------------------------------------------------
# Source record verification
# ---------------------------------------------------------------------------


class TestSourceRecord:
    async def test_source_type_is_ucdp(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_single_page),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        src: Source = records[0]["_source"]
        assert src.source_type is SourceType.UCDP

    async def test_same_source_shared_within_page(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_single_page),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        source_ids = {id(r["_source"]) for r in records}
        assert len(source_ids) == 1

    async def test_source_attribution_present(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_single_page),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        src: Source = records[0]["_source"]
        assert src.metadata["attribution"] == UCDP_ATTRIBUTION

    async def test_source_content_hash_is_sha256(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_single_page),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        src: Source = records[0]["_source"]
        assert len(src.content_hash) == 64
        assert all(c in "0123456789abcdef" for c in src.content_hash)

    async def test_retrieved_by(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_handler_single_page),
        )
        try:
            async with UCDPConnector(client=client) as conn:
                records = [
                    r async for r in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    )
                ]
        finally:
            await client.aclose()
        src: Source = records[0]["_source"]
        assert src.retrieved_by == "wced.ingest.ucdp"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestConnectorErrors:
    async def test_no_client_raises_runtime_error(self) -> None:
        conn = UCDPConnector()
        with pytest.raises(RuntimeError, match="context manager"):
            async for _ in conn.query_events(date(2026, 3, 1), date(2026, 3, 31)):
                pass

    async def test_4xx_raises_immediately(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text='{"error":"Not Found"}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            async with UCDPConnector(client=client, max_attempts=2) as conn:
                with pytest.raises(UCDPError, match="404"):
                    async for _ in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    ):
                        pass
        finally:
            await client.aclose()

    async def test_non_json_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="this is not json")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            async with UCDPConnector(client=client, max_attempts=1) as conn:
                with pytest.raises(UCDPError, match="not JSON"):
                    async for _ in conn.query_events(
                        date(2026, 3, 1), date(2026, 3, 31),
                    ):
                        pass
        finally:
            await client.aclose()
