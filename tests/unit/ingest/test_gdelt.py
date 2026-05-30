"""Tests for wced.ingest.gdelt.

VCR cassettes under tests/fixtures/cassettes/ record the GDELT DOC API
responses. Cassettes match on method + scheme + host + path only (no query)
because GDELT puts everything in query params and encoding varies.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest
import vcr

from wced.ingest.gdelt import (
    DEFAULT_DOC_QUERY,
    GDELT_ATTRIBUTION,
    GDELT_MAX_CONFIDENCE,
    VIOLENT_ROOT_CODES,
    GDELTConnector,
    GDELTError,
    GDELTEvent,
    _content_hash,
    _parse_csv_row,
    _parse_doc_article,
)
from wced.models.provenance import ConfidenceLabel, Source, SourceType
from pathlib import Path

CASSETTE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "cassettes"

_vcr = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    record_mode="none",
    match_on=("method", "scheme", "host", "path"),
)

QUERY_DATE = datetime(2026, 3, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# GDELTEvent model tests
# ---------------------------------------------------------------------------


class TestGDELTEvent:
    def test_is_violent_with_assault_code(self) -> None:
        event = GDELTEvent(
            event_id="123",
            event_date=date(2026, 3, 15),
            event_type="183",
            event_root_code="18",
            actor1="A",
            actor2="B",
            latitude=32.66,
            longitude=51.68,
            source_url="https://example.com",
            num_articles=5,
            avg_tone=-3.0,
            goldstein_scale=-7.0,
            detected_at=datetime(2026, 3, 15, tzinfo=UTC),
        )
        assert event.is_violent is True

    def test_is_violent_with_fight_code(self) -> None:
        event = GDELTEvent(
            event_id="124",
            event_date=date(2026, 3, 15),
            event_type="194",
            event_root_code="19",
            actor1="A",
            actor2="B",
            latitude=32.66,
            longitude=51.68,
            source_url="https://example.com",
            num_articles=3,
            avg_tone=-4.0,
            goldstein_scale=-8.0,
            detected_at=datetime(2026, 3, 15, tzinfo=UTC),
        )
        assert event.is_violent is True

    def test_is_violent_with_mass_violence_code(self) -> None:
        event = GDELTEvent(
            event_id="125",
            event_date=date(2026, 3, 15),
            event_type="201",
            event_root_code="20",
            actor1="A",
            actor2="B",
            latitude=32.66,
            longitude=51.68,
            source_url="https://example.com",
            num_articles=10,
            avg_tone=-6.0,
            goldstein_scale=-10.0,
            detected_at=datetime(2026, 3, 15, tzinfo=UTC),
        )
        assert event.is_violent is True

    def test_not_violent_with_diplomatic_code(self) -> None:
        event = GDELTEvent(
            event_id="126",
            event_date=date(2026, 3, 15),
            event_type="042",
            event_root_code="04",
            actor1="A",
            actor2="B",
            latitude=32.66,
            longitude=51.68,
            source_url="https://example.com",
            num_articles=2,
            avg_tone=1.0,
            goldstein_scale=3.0,
            detected_at=datetime(2026, 3, 15, tzinfo=UTC),
        )
        assert event.is_violent is False

    def test_model_is_frozen(self) -> None:
        event = GDELTEvent(
            event_id="127",
            event_date=date(2026, 3, 15),
            event_type="183",
            event_root_code="18",
            actor1="A",
            actor2="B",
            latitude=32.66,
            longitude=51.68,
            source_url="https://example.com",
            num_articles=1,
            avg_tone=0.0,
            goldstein_scale=0.0,
            detected_at=datetime(2026, 3, 15, tzinfo=UTC),
        )
        with pytest.raises(Exception):
            event.actor1 = "C"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CSV row parsing
# ---------------------------------------------------------------------------


class TestParseCSVRow:
    def test_parses_valid_row(self) -> None:
        row = {
            "GLOBALEVENTID": "987654321",
            "SQLDATE": "20260315",
            "EventCode": "190",
            "EventRootCode": "19",
            "Actor1Name": "ISRAEL",
            "Actor2Name": "IRAN",
            "ActionGeo_Lat": "32.6601",
            "ActionGeo_Long": "51.6860",
            "ActionGeo_CountryCode": "IR",
            "SOURCEURL": "https://reuters.com/article/123",
            "NumArticles": "7",
            "AvgTone": "-5.2",
            "GoldsteinScale": "-7.0",
        }
        event = _parse_csv_row(row)
        assert event is not None
        assert event.event_id == "987654321"
        assert event.event_date == date(2026, 3, 15)
        assert event.event_root_code == "19"
        assert event.latitude == pytest.approx(32.6601)
        assert event.longitude == pytest.approx(51.6860)
        assert event.num_articles == 7
        assert event.avg_tone == pytest.approx(-5.2)
        assert event.goldstein_scale == pytest.approx(-7.0)
        assert event.is_violent is True

    def test_rejects_zero_coordinates(self) -> None:
        row = {
            "GLOBALEVENTID": "1",
            "SQLDATE": "20260315",
            "EventCode": "190",
            "EventRootCode": "19",
            "Actor1Name": "A",
            "Actor2Name": "B",
            "ActionGeo_Lat": "0",
            "ActionGeo_Long": "0",
            "SOURCEURL": "https://example.com",
            "NumArticles": "1",
            "AvgTone": "0",
            "GoldsteinScale": "0",
        }
        assert _parse_csv_row(row) is None

    def test_rejects_invalid_date(self) -> None:
        row = {
            "GLOBALEVENTID": "2",
            "SQLDATE": "bad",
            "EventCode": "190",
            "EventRootCode": "19",
            "Actor1Name": "A",
            "Actor2Name": "B",
            "ActionGeo_Lat": "32.0",
            "ActionGeo_Long": "51.0",
            "SOURCEURL": "https://example.com",
            "NumArticles": "1",
            "AvgTone": "0",
            "GoldsteinScale": "0",
        }
        assert _parse_csv_row(row) is None

    def test_detected_at_is_midnight_utc(self) -> None:
        row = {
            "GLOBALEVENTID": "3",
            "SQLDATE": "20260315",
            "EventCode": "183",
            "EventRootCode": "18",
            "Actor1Name": "A",
            "Actor2Name": "B",
            "ActionGeo_Lat": "35.7",
            "ActionGeo_Long": "51.4",
            "SOURCEURL": "https://example.com",
            "NumArticles": "1",
            "AvgTone": "0",
            "GoldsteinScale": "0",
        }
        event = _parse_csv_row(row)
        assert event is not None
        assert event.detected_at == datetime(2026, 3, 15, 0, 0, 0, tzinfo=UTC)
        assert event.detected_at.tzinfo is UTC


# ---------------------------------------------------------------------------
# DOC API article parsing
# ---------------------------------------------------------------------------


class TestParseDocArticle:
    def test_parses_article_with_geo(self) -> None:
        article = {
            "url": "https://reuters.com/article/isfahan-strike",
            "title": "Isfahan refinery struck",
            "seendate": "20260315T120000Z",
            "domain": "reuters.com",
            "sourcecountylat": "32.6601",
            "sourcecountylon": "51.6860",
            "tone": "-5.2",
            "sharingimage_maxlinks": "12",
        }
        event = _parse_doc_article(article)
        assert event is not None
        assert event.latitude == pytest.approx(32.6601)
        assert event.longitude == pytest.approx(51.6860)
        assert event.event_date == date(2026, 3, 15)
        assert event.source_url == "https://reuters.com/article/isfahan-strike"

    def test_rejects_missing_geo(self) -> None:
        article = {
            "url": "https://example.com",
            "title": "No geo",
            "seendate": "20260315T120000Z",
            "domain": "example.com",
        }
        assert _parse_doc_article(article) is None


# ---------------------------------------------------------------------------
# Connector — VCR-replayed tests
# ---------------------------------------------------------------------------


async def _collect_doc_api(
    start: datetime, end: datetime,
) -> list[dict]:
    async with GDELTConnector() as c:
        return [r async for r in c.query_events_api(start=start, end=end)]


class TestDocAPIHappyPath:
    async def test_parses_three_articles(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_articles.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        assert len(records) == 3

    async def test_first_record_fields(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_articles.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        first = records[0]
        event: GDELTEvent = first["event"]
        assert event.latitude == pytest.approx(32.6601)
        assert event.longitude == pytest.approx(51.6860)
        assert "reuters.com" in event.source_url

    async def test_detected_at_injected(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_articles.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        for rec in records:
            assert rec["detected_at"] == datetime(2026, 3, 15, tzinfo=UTC)
            assert rec["detected_at"] is rec["event"].detected_at

    async def test_empty_response_yields_zero(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_empty.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        assert records == []

    async def test_retries_on_503(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_503_then_200.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        assert len(records) == 1
        assert "reuters.com" in records[0]["event"].source_url


# ---------------------------------------------------------------------------
# Source record verification
# ---------------------------------------------------------------------------


class TestSourceRecord:
    async def test_source_type_is_gdelt(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_articles.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert src.source_type is SourceType.GDELT

    async def test_same_source_shared_within_response(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_articles.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        source_ids = {id(r["_source"]) for r in records}
        assert len(source_ids) == 1

    async def test_source_attribution_present(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_articles.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert src.metadata["attribution"] == GDELT_ATTRIBUTION

    async def test_source_content_hash_is_sha256(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_articles.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert len(src.content_hash) == 64
        assert all(c in "0123456789abcdef" for c in src.content_hash)

    async def test_retrieved_by(self) -> None:
        with _vcr.use_cassette("gdelt_doc_api_articles.yaml"):
            records = await _collect_doc_api(QUERY_DATE, QUERY_DATE)
        src: Source = records[0]["_source"]
        assert src.retrieved_by == "wced.ingest.gdelt"


# ---------------------------------------------------------------------------
# Connector construction
# ---------------------------------------------------------------------------


class TestConnectorBasics:
    async def test_no_client_raises_runtime_error(self) -> None:
        c = GDELTConnector()
        with pytest.raises(RuntimeError, match="context manager"):
            async for _ in c.query_events_api():
                pass

    async def test_4xx_raises_immediately(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text='{"error":"Forbidden"}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            c = GDELTConnector(client=client, max_attempts=3)
            async with c:
                with pytest.raises(GDELTError, match="403"):
                    async for _ in c.query_events_api():
                        pass
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# GDELT max confidence constant
# ---------------------------------------------------------------------------


class TestMaxConfidence:
    def test_gdelt_max_confidence_is_reported(self) -> None:
        assert GDELT_MAX_CONFIDENCE is ConfidenceLabel.REPORTED

    def test_gdelt_cannot_reach_confirmed(self) -> None:
        # CONFIRMED is stronger than REPORTED — GDELT alone should never
        # produce CONFIRMED. This is enforced in confidence.py decision table.
        assert GDELT_MAX_CONFIDENCE is not ConfidenceLabel.CONFIRMED
        assert GDELT_MAX_CONFIDENCE is not ConfidenceLabel.VERIFIED


# ---------------------------------------------------------------------------
# datetime formatting
# ---------------------------------------------------------------------------


class TestDatetimeFormatting:
    def test_format_datetime(self) -> None:
        c = GDELTConnector()
        dt = datetime(2026, 3, 15, 14, 30, 0, tzinfo=UTC)
        assert c._format_datetime(dt) == "20260315143000"

    def test_format_date(self) -> None:
        c = GDELTConnector()
        d = date(2026, 3, 15)
        assert c._format_datetime(d) == "20260315000000"


# ---------------------------------------------------------------------------
# lastupdate.txt parsing
# ---------------------------------------------------------------------------


class TestExtractExportUrl:
    def test_extracts_export_url(self) -> None:
        text = (
            "12345 abc123 http://data.gdeltproject.org/gdeltv2/20260315143000.export.CSV.zip\n"
            "6789 def456 http://data.gdeltproject.org/gdeltv2/20260315143000.mentions.CSV.zip\n"
            "1011 ghi789 http://data.gdeltproject.org/gdeltv2/20260315143000.gkg.csv.zip\n"
        )
        url = GDELTConnector._extract_export_url(text)
        assert url == "http://data.gdeltproject.org/gdeltv2/20260315143000.export.CSV.zip"

    def test_returns_none_for_empty(self) -> None:
        assert GDELTConnector._extract_export_url("") is None

    def test_returns_none_for_no_export(self) -> None:
        text = "12345 abc123 http://example.com/no-match.csv\n"
        assert GDELTConnector._extract_export_url(text) is None
