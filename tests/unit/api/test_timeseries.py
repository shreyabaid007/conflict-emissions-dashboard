"""Tests for /v1/timeseries endpoints."""
from __future__ import annotations


class TestDailyTimeseries:
    def test_returns_daily_points(self, client, seed_data):
        resp = client.get("/v1/timeseries/daily")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 2
        for pt in body["data"]:
            assert "p5" in pt and "p50" in pt and "p95" in pt

    def test_date_filter(self, client, seed_data):
        resp = client.get("/v1/timeseries/daily", params={"from": "2026-03-06"})
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1


class TestCumulativeTimeseries:
    def test_running_sum(self, client, seed_data):
        resp = client.get("/v1/timeseries/cumulative")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2
        assert data[1]["p50"] == 1200.0 + 500.0
