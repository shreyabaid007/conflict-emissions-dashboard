"""Tests for wced.ingest.firms.

Replay is driven by vcrpy cassettes under tests/fixtures/cassettes/ — see
that directory for the recorded FIRMS responses. The cassettes pin URLs that
include a fake MAP_KEY ("TESTKEY") so the connector's URL construction is
exercised end-to-end.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import vcr

from wced.ingest import firms as firms_mod
from wced.ingest.firms import (
    FIRMSConnector,
    FIRMSError,
    _coerce_record,
    _iter_chunks,
    _parse_acq_datetime,
)
from wced.models.provenance import Source, SourceType

CASSETTE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "cassettes"

_vcr = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    record_mode="none",
    match_on=("method", "scheme", "host", "path", "query"),
)

ISFAHAN_BBOX = (50.0, 32.0, 52.0, 34.0)
QUERY_DAY = datetime(2026, 3, 15, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _single_viirs_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin VIIRS to one platform so cassettes do not need 3× the interactions."""
    monkeypatch.setattr(firms_mod, "VIIRS_SOURCES", ("VIIRS_SNPP_NRT",))


# ---------------------------------------------------------------------------
# pure-function helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_parse_acq_datetime_pads_hhmm(self) -> None:
        ts = _parse_acq_datetime("2026-03-15", "823")
        assert ts == datetime(2026, 3, 15, 8, 23, tzinfo=UTC)

    def test_parse_acq_datetime_utc(self) -> None:
        ts = _parse_acq_datetime("2026-03-15", "1402")
        assert ts.tzinfo is UTC
        assert (ts.hour, ts.minute) == (14, 2)

    def test_coerce_record_renames_brightness_for_viirs(self) -> None:
        row = {
            "latitude": "32.66",
            "longitude": "51.67",
            "bright_ti4": "325.4",
            "scan": "0.39",
            "track": "0.36",
            "acq_date": "2026-03-15",
            "acq_time": "0823",
            "satellite": "N",
            "confidence": "n",
            "version": "2.0NRT",
            "frp": "15.7",
            "daynight": "D",
        }
        out = _coerce_record(row, "bright_ti4")
        assert "bright_ti4" not in out
        assert out["brightness"] == 325.4
        assert out["latitude"] == 32.66
        assert out["frp"] == 15.7
        assert out["confidence"] == "n"  # VIIRS confidence stays as string
        assert out["detected_at"] == datetime(2026, 3, 15, 8, 23, tzinfo=UTC)

    def test_coerce_record_passthrough_for_modis(self) -> None:
        row = {
            "latitude": "32.66",
            "longitude": "51.67",
            "brightness": "330.1",
            "scan": "1.0",
            "track": "1.0",
            "acq_date": "2026-03-15",
            "acq_time": "0823",
            "satellite": "Terra",
            "confidence": "80",
            "version": "6.1NRT",
            "frp": "22.4",
            "daynight": "D",
        }
        out = _coerce_record(row, "brightness")
        assert out["brightness"] == 330.1

    def test_iter_chunks_splits_long_windows(self) -> None:
        start = datetime(2026, 3, 1, tzinfo=UTC)
        end = datetime(2026, 3, 25, tzinfo=UTC)
        chunks = list(_iter_chunks(start, end))
        spans = [span for _, span in chunks]
        assert sum(spans) == 25
        assert all(s <= 10 for s in spans)
        # last anchor must equal end's date
        assert chunks[-1][0] == end.date()

    def test_iter_chunks_single_day(self) -> None:
        start = end = datetime(2026, 3, 15, tzinfo=UTC)
        assert list(_iter_chunks(start, end)) == [(start.date(), 1)]

    def test_iter_chunks_rejects_inverted_window(self) -> None:
        with pytest.raises(ValueError, match="must be >="):
            list(
                _iter_chunks(
                    datetime(2026, 3, 15, tzinfo=UTC),
                    datetime(2026, 3, 1, tzinfo=UTC),
                )
            )


# ---------------------------------------------------------------------------
# connector smoke
# ---------------------------------------------------------------------------


class TestConnectorBasics:
    def test_rejects_empty_map_key(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            FIRMSConnector(map_key="")

    async def test_rejects_naive_datetimes(self) -> None:
        async with FIRMSConnector(map_key="TESTKEY") as c:
            with pytest.raises(ValueError, match="timezone-aware"):
                async for _ in c.ingest_viirs(
                    datetime(2026, 3, 15), datetime(2026, 3, 15), ISFAHAN_BBOX
                ):
                    pass

    def test_url_construction(self) -> None:
        c = FIRMSConnector(map_key="TESTKEY")
        url = c._build_url(
            "VIIRS_SNPP_NRT", ISFAHAN_BBOX, QUERY_DAY.date(), 1
        )
        assert url == (
            "https://firms.modaps.eosdis.nasa.gov/api/area/csv/TESTKEY/"
            "VIIRS_SNPP_NRT/50.0,32.0,52.0,34.0/1/2026-03-15"
        )


# ---------------------------------------------------------------------------
# VCR-replayed integration of the public ingest path
# ---------------------------------------------------------------------------


async def _collect_viirs(bbox: tuple[float, float, float, float]) -> list[dict]:
    async with FIRMSConnector(map_key="TESTKEY") as c:
        return [r async for r in c.ingest_viirs(QUERY_DAY, QUERY_DAY, bbox)]


class TestIngestViirs:
    async def test_parses_real_response(self) -> None:
        with _vcr.use_cassette("firms_viirs_isfahan.yaml"):
            records = await _collect_viirs(ISFAHAN_BBOX)
        assert len(records) == 3

        first = records[0]
        assert first["latitude"] == pytest.approx(32.6643)
        assert first["longitude"] == pytest.approx(51.6783)
        assert first["brightness"] == pytest.approx(325.4)
        assert first["frp"] == pytest.approx(15.7)
        assert first["confidence"] == "n"
        assert first["daynight"] == "D"
        assert first["detected_at"] == datetime(2026, 3, 15, 8, 23, tzinfo=UTC)
        assert "bright_ti4" not in first  # normalised away

        # high-confidence row is preserved verbatim
        assert records[1]["confidence"] == "h"
        # night detection has expected acq_time
        assert records[2]["detected_at"] == datetime(2026, 3, 15, 14, 2, tzinfo=UTC)

    async def test_empty_bbox_yields_zero_records(self) -> None:
        with _vcr.use_cassette("firms_empty_bbox.yaml"):
            records = await _collect_viirs((0.0, 0.0, 0.1, 0.1))
        assert records == []

    async def test_retries_on_503(self) -> None:
        with _vcr.use_cassette("firms_503_then_200.yaml"):
            records = await _collect_viirs(ISFAHAN_BBOX)
        assert len(records) == 1
        assert records[0]["latitude"] == pytest.approx(32.6643)


# ---------------------------------------------------------------------------
# Source record verification
# ---------------------------------------------------------------------------


class TestSourceRecord:
    async def test_source_attached_with_correct_hash(self) -> None:
        with _vcr.use_cassette("firms_viirs_isfahan.yaml"):
            records = await _collect_viirs(ISFAHAN_BBOX)

        # The same Source must be shared by every record from one response.
        sources = {id(r["_source"]) for r in records}
        assert len(sources) == 1

        src: Source = records[0]["_source"]
        assert isinstance(src, Source)
        assert src.source_type is SourceType.SATELLITE
        assert src.identifier.startswith(
            "https://firms.modaps.eosdis.nasa.gov/api/area/csv/TESTKEY/VIIRS_SNPP_NRT/"
        )
        assert src.retrieved_by == "wced.ingest.firms:VIIRS_SNPP_NRT"
        assert src.metadata["firms_source"] == "VIIRS_SNPP_NRT"
        assert src.metadata["bbox"] == list(ISFAHAN_BBOX)
        assert src.metadata["anchor_date"] == "2026-03-15"
        assert src.metadata["day_range"] == 1

        # Hash must match the cassette body byte-for-byte.
        cassette_body = (
            "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,"
            "satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
            "32.6643,51.6783,325.4,0.39,0.36,2026-03-15,0823,N,VIIRS,n,2.0NRT,295.1,15.7,D\n"
            "32.6651,51.6802,338.2,0.39,0.36,2026-03-15,0823,N,VIIRS,h,2.0NRT,301.5,42.3,D\n"
            "32.6800,51.6900,312.0,0.41,0.37,2026-03-15,1402,N,VIIRS,l,2.0NRT,289.2,5.4,N\n"
        ).encode()
        assert src.content_hash == hashlib.sha256(cassette_body).hexdigest()


# ---------------------------------------------------------------------------
# error surface
# ---------------------------------------------------------------------------


class TestErrors:
    async def test_4xx_raises_immediately(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="bad key")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            c = FIRMSConnector(map_key="BAD", client=client, max_attempts=3)
            async with c:
                with pytest.raises(FIRMSError, match="401"):
                    async for _ in c.ingest_viirs(QUERY_DAY, QUERY_DAY, ISFAHAN_BBOX):
                        pass
        finally:
            await client.aclose()

    async def test_html_payload_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text="<html><body>oops</body></html>", headers={"content-type": "text/html"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            c = FIRMSConnector(map_key="TESTKEY", client=client)
            async with c:
                with pytest.raises(FIRMSError, match="non-CSV"):
                    async for _ in c.ingest_viirs(QUERY_DAY, QUERY_DAY, ISFAHAN_BBOX):
                        pass
        finally:
            await client.aclose()
