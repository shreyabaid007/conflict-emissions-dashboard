"""Tests for /v1/aggregates endpoints."""
from __future__ import annotations


class TestByFacilityType:
    def test_groups_by_type(self, client, seed_data):
        resp = client.get("/v1/aggregates/by_facility_type")
        assert resp.status_code == 200
        data = resp.json()["data"]
        keys = {r["key"] for r in data}
        assert "REFINERY" in keys
        assert "OIL_DEPOT" in keys


class TestByCountry:
    def test_groups_by_country(self, client, seed_data):
        resp = client.get("/v1/aggregates/by_country")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["key"] == "IRN"
        assert data[0]["p50"] == 1700.0
