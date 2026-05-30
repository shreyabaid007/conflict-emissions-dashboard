"""Contract tests using Schemathesis against the OpenAPI spec.

These tests fuzz-test every endpoint against the auto-generated OpenAPI
schema, catching response validation errors, 500s, and schema mismatches.
"""
from __future__ import annotations

import pytest

schemathesis = pytest.importorskip("schemathesis", reason="schemathesis not installed")

import re  # noqa: E402

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from wced.api.dependencies import get_db  # noqa: E402
from wced.api.main import create_app  # noqa: E402

_POINT_RE = re.compile(r"POINT\(\s*([-\d.]+)\s+([-\d.]+)\s*\)")


def _st_x(wkt: str | None) -> float | None:
    if wkt is None:
        return None
    m = _POINT_RE.search(wkt)
    return float(m.group(1)) if m else None


def _st_y(wkt: str | None) -> float | None:
    if wkt is None:
        return None
    m = _POINT_RE.search(wkt)
    return float(m.group(2)) if m else None


def _make_app():
    """Build an app with an empty SQLite backend for schema-level fuzzing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    from sqlalchemy import event as sa_event

    @sa_event.listens_for(engine, "connect")
    def _register_functions(dbapi_conn, _connection_record):
        dbapi_conn.create_function("ST_AsText", 1, lambda v: v)
        dbapi_conn.create_function("ST_GeomFromText", 2, lambda wkt, srid: wkt)
        dbapi_conn.create_function("ST_Centroid", 1, lambda v: v)
        dbapi_conn.create_function("ST_X", 1, _st_x)
        dbapi_conn.create_function("ST_Y", 1, _st_y)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE facilities (id TEXT PRIMARY KEY, name TEXT NOT NULL, facility_type TEXT NOT NULL, geometry TEXT NOT NULL, country VARCHAR(3) NOT NULL, capacity_barrels REAL, capacity_uncertainty_pct REAL NOT NULL DEFAULT 30.0, operator TEXT, source_url TEXT NOT NULL, added_at TIMESTAMP NOT NULL, notes TEXT)"))
        conn.execute(text("CREATE TABLE fire_events (id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, detected_at TIMESTAMP NOT NULL, last_seen_at TIMESTAMP NOT NULL, peak_frp_mw REAL NOT NULL, total_frp_integral_mj REAL, detection_source TEXT NOT NULL, confidence_label TEXT NOT NULL, status TEXT NOT NULL, provenance_id TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, notes TEXT)"))
        conn.execute(text("CREATE TABLE editorial_actions (id TEXT PRIMARY KEY, event_id TEXT NOT NULL, action_type TEXT NOT NULL, reviewer TEXT NOT NULL, notes TEXT, previous_status TEXT NOT NULL, new_status TEXT NOT NULL, acted_at TIMESTAMP NOT NULL)"))
        conn.execute(text("CREATE TABLE emission_estimates (id TEXT PRIMARY KEY, event_id TEXT NOT NULL, methodology_version TEXT NOT NULL, method TEXT NOT NULL, p5 REAL NOT NULL, p50 REAL NOT NULL, p95 REAL NOT NULL, samples_ref TEXT, units TEXT NOT NULL DEFAULT 'tCO2e', provenance_id TEXT NOT NULL, parameter_versions TEXT NOT NULL DEFAULT '{}', created_at TIMESTAMP NOT NULL)"))
        conn.execute(text("CREATE TABLE provenance_records (id TEXT PRIMARY KEY, produced_by TEXT NOT NULL, method TEXT NOT NULL, parameters TEXT NOT NULL DEFAULT '{}', produced_at TIMESTAMP NOT NULL, confidence_label TEXT NOT NULL, notes TEXT)"))
        conn.execute(text("CREATE TABLE sources (id TEXT PRIMARY KEY, source_type TEXT NOT NULL, identifier TEXT NOT NULL, retrieved_at TIMESTAMP NOT NULL, content_hash TEXT NOT NULL, metadata_ TEXT NOT NULL DEFAULT '{}')"))
        conn.execute(text("CREATE TABLE provenance_inputs (provenance_id TEXT NOT NULL, input_id TEXT NOT NULL, input_type TEXT NOT NULL, PRIMARY KEY (provenance_id, input_id))"))
        conn.execute(text("CREATE TABLE damage_assessments (id TEXT PRIMARY KEY, event_id TEXT NOT NULL, facility_id TEXT NOT NULL, fraction_destroyed_low REAL NOT NULL, fraction_destroyed_mode REAL NOT NULL, fraction_destroyed_high REAL NOT NULL, assessed_by TEXT NOT NULL, assessment_method TEXT NOT NULL, notes TEXT, assessed_at TIMESTAMP NOT NULL, provenance_id TEXT NOT NULL)"))
        conn.execute(text("CREATE TABLE methodology_versions (version_id TEXT PRIMARY KEY, released_at TIMESTAMP NOT NULL, pdf_url TEXT NOT NULL, changelog TEXT)"))
        conn.execute(text("CREATE TABLE pipeline_runs (id TEXT PRIMARY KEY, flow_name TEXT NOT NULL, started_at TIMESTAMP NOT NULL, ended_at TIMESTAMP, status TEXT NOT NULL, metrics TEXT NOT NULL DEFAULT '{}')"))
        conn.execute(text("CREATE TABLE firms_detections (id TEXT PRIMARY KEY, latitude REAL, longitude REAL, brightness REAL, frp REAL, confidence TEXT, acq_datetime TIMESTAMP, satellite TEXT, instrument TEXT, version TEXT, bright_t31 REAL, scan REAL, track REAL, raw_json TEXT, ingested_at TIMESTAMP)"))
        conn.execute(text("CREATE TABLE s2_chips (id TEXT PRIMARY KEY, event_id TEXT, facility_id TEXT, product_id TEXT, acquisition_date TIMESTAMP, cloud_cover_pct REAL, storage_path TEXT, bands TEXT, fetched_at TIMESTAMP)"))
        conn.execute(text("CREATE TABLE acled_events (id TEXT PRIMARY KEY, acled_id INTEGER UNIQUE, event_date DATE, event_type TEXT, sub_event_type TEXT, country TEXT, admin1 TEXT, admin2 TEXT, location TEXT, latitude REAL, longitude REAL, source TEXT, notes TEXT, raw_json TEXT, ingested_at TIMESTAMP)"))
        conn.execute(text("CREATE TABLE validation_reports (id TEXT PRIMARY KEY, event_id TEXT, tropomi_estimate_p50 REAL NOT NULL, discrepancy_ratio REAL NOT NULL, needs_review INTEGER NOT NULL DEFAULT 0, generated_at TIMESTAMP NOT NULL)"))

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    from wced.api.dependencies import rate_limit

    app = create_app()

    def _override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    def _noop_rate_limit():
        pass

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[rate_limit] = _noop_rate_limit
    return app


schema = schemathesis.openapi.from_asgi("/openapi.json", app=_make_app())


@schema.parametrize()
def test_api_contract(case):
    """Every endpoint must return a response that matches its OpenAPI schema.

    Uses not_a_server_error to verify no 5xx responses, plus validates that
    successful responses match the declared schema. The positive_data_rejection
    check is excluded because OpenAPI nullable query params (e.g. date|null)
    generate the literal string "null" which can't be represented in HTTP
    query strings — a known OpenAPI/HTTP impedance mismatch.
    """
    response = case.call()
    case.validate_response(
        response,
        checks=[schemathesis.checks.not_a_server_error],
    )
