"""Tests for methodology, changelog, and health endpoints."""
from __future__ import annotations


class TestMethodology:
    def test_returns_current_version(self, client, seed_data):
        resp = client.get("/v1/methodology/current")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version_id"] == "1.0"
        assert body["pdf_url"] == "/methodology/v1.0.pdf"


class TestChangelog:
    def test_includes_methodology_releases(self, client, seed_data):
        resp = client.get("/v1/changelog")
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        assert any(e["change_type"] == "methodology_release" for e in entries)


class TestHealth:
    def test_ok(self, client, seed_data):
        resp = client.get("/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"


class TestEnvelopeFields:
    def test_all_responses_include_envelope(self, client, seed_data):
        endpoints = [
            "/v1/events",
            "/v1/timeseries/daily",
            "/v1/aggregates/by_country",
            "/v1/methodology/current",
            "/v1/changelog",
        ]
        for url in endpoints:
            resp = client.get(url)
            assert resp.status_code == 200, f"{url} returned {resp.status_code}"
            body = resp.json()
            assert "methodology_version" in body, f"{url} missing methodology_version"
            assert "generated_at" in body, f"{url} missing generated_at"
            assert "data_license" in body, f"{url} missing data_license"
            assert "attribution" in body, f"{url} missing attribution"
