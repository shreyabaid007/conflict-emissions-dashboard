"""Tests for /v1/facilities endpoints."""
from __future__ import annotations

from uuid import uuid4


class TestListFacilities:
    def test_returns_all(self, client, seed_data):
        resp = client.get("/v1/facilities")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pagination"]["total"] == 2

    def test_filter_by_country(self, client, seed_data):
        resp = client.get("/v1/facilities", params={"country": "IRN"})
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 2

        resp = client.get("/v1/facilities", params={"country": "USA"})
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 0

    def test_filter_by_type(self, client, seed_data):
        resp = client.get("/v1/facilities", params={"facility_type": "REFINERY"})
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 1


class TestGetFacility:
    def test_returns_detail(self, client, seed_data):
        fid = seed_data["facility_id"]
        resp = client.get(f"/v1/facilities/{fid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["name"] == "Abadan Refinery"
        assert body["event_count"] == 1
        assert body["total_p50_tco2e"] == 1200.0

    def test_not_found(self, client, seed_data):
        resp = client.get(f"/v1/facilities/{uuid4()}")
        assert resp.status_code == 404
