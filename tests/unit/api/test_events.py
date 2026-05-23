"""Tests for /v1/events endpoints."""
from __future__ import annotations

from uuid import uuid4


class TestListEvents:
    def test_returns_paginated_events(self, client, seed_data):
        resp = client.get("/v1/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_license"] == "CC-BY 4.0"
        assert body["methodology_version"] == "1.0.5"
        assert body["pagination"]["total"] == 2
        assert len(body["data"]) == 2

    def test_filter_by_status(self, client, seed_data):
        resp = client.get("/v1/events", params={"status": "PUBLISHED"})
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 2

        resp = client.get("/v1/events", params={"status": "PENDING_REVIEW"})
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 0

    def test_filter_by_date_range(self, client, seed_data):
        resp = client.get("/v1/events", params={"from": "2026-03-06", "to": "2026-03-15"})
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 1

    def test_filter_by_facility_type(self, client, seed_data):
        resp = client.get("/v1/events", params={"facility_type": "REFINERY"})
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 1

    def test_event_includes_estimate(self, client, seed_data):
        resp = client.get("/v1/events")
        body = resp.json()
        events_with_est = [e for e in body["data"] if e["estimate"] is not None]
        assert len(events_with_est) == 2

    def test_pagination(self, client, seed_data):
        resp = client.get("/v1/events", params={"per_page": 1, "page": 1})
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["pagination"]["pages"] == 2


class TestGetEvent:
    def test_returns_event_with_estimates(self, client, seed_data):
        eid = seed_data["event_id"]
        resp = client.get(f"/v1/events/{eid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["id"] == eid
        assert len(body["estimates"]) == 1
        assert body["estimates"][0]["p50"] == 1200.0

    def test_not_found(self, client, seed_data):
        resp = client.get(f"/v1/events/{uuid4()}")
        assert resp.status_code == 404


class TestProvenance:
    def test_returns_provenance_chain(self, client, seed_data):
        eid = seed_data["event_id"]
        resp = client.get(f"/v1/events/{eid}/provenance")
        assert resp.status_code == 200
        body = resp.json()
        assert body["event_id"] == eid
        assert len(body["chain"]) >= 1
        assert "rendered" in body

    def test_not_found(self, client, seed_data):
        resp = client.get(f"/v1/events/{uuid4()}/provenance")
        assert resp.status_code == 404


class TestAssessment:
    def test_no_assessment_returns_null_data(self, client, seed_data):
        eid = seed_data["event_id"]
        resp = client.get(f"/v1/events/{eid}/assessment")
        assert resp.status_code == 200
        assert resp.json()["data"] is None

    def test_not_found(self, client, seed_data):
        resp = client.get(f"/v1/events/{uuid4()}/assessment")
        assert resp.status_code == 404
