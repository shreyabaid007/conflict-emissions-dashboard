"""Fixtures for API tests — in-memory SQLite database + TestClient."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from wced.api.dependencies import get_db
from wced.api.main import create_app
from wced.db import models

import re

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


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Register a listener so ST_AsText(col) just returns the column value on
    # SQLite (which stores geometry as plain text already).
    @sa_event.listens_for(engine, "connect")
    def _register_functions(dbapi_conn, _connection_record):
        dbapi_conn.create_function("ST_AsText", 1, lambda v: v)
        dbapi_conn.create_function("ST_GeomFromText", 2, lambda wkt, srid: wkt)
        dbapi_conn.create_function("ST_Centroid", 1, lambda v: v)
        dbapi_conn.create_function("ST_X", 1, _st_x)
        dbapi_conn.create_function("ST_Y", 1, _st_y)

    # Create tables using DDL that SQLite understands.
    # We replicate the schema from models.metadata but replace Geometry with Text.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE facilities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                facility_type TEXT NOT NULL,
                geometry TEXT NOT NULL,
                country VARCHAR(3) NOT NULL,
                capacity_barrels REAL,
                capacity_uncertainty_pct REAL NOT NULL DEFAULT 30.0,
                operator TEXT,
                source_url TEXT NOT NULL,
                added_at TIMESTAMP NOT NULL,
                notes TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE fire_events (
                id TEXT PRIMARY KEY,
                facility_id TEXT NOT NULL REFERENCES facilities(id),
                detected_at TIMESTAMP NOT NULL,
                last_seen_at TIMESTAMP NOT NULL,
                peak_frp_mw REAL NOT NULL,
                total_frp_integral_mj REAL,
                detection_source TEXT NOT NULL,
                confidence_label TEXT NOT NULL,
                status TEXT NOT NULL,
                provenance_id TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                notes TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE editorial_actions (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES fire_events(id),
                action_type TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                notes TEXT,
                previous_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                acted_at TIMESTAMP NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE emission_estimates (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES fire_events(id),
                methodology_version TEXT NOT NULL,
                method TEXT NOT NULL,
                p5 REAL NOT NULL,
                p50 REAL NOT NULL,
                p95 REAL NOT NULL,
                samples_ref TEXT,
                units TEXT NOT NULL DEFAULT 'tCO2e',
                provenance_id TEXT NOT NULL,
                parameter_versions TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE provenance_records (
                id TEXT PRIMARY KEY,
                produced_by TEXT NOT NULL,
                method TEXT NOT NULL,
                parameters TEXT NOT NULL DEFAULT '{}',
                produced_at TIMESTAMP NOT NULL,
                confidence_label TEXT NOT NULL,
                notes TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE sources (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                identifier TEXT NOT NULL,
                retrieved_at TIMESTAMP NOT NULL,
                content_hash TEXT NOT NULL,
                metadata_ TEXT NOT NULL DEFAULT '{}'
            )
        """))
        conn.execute(text("""
            CREATE TABLE provenance_inputs (
                provenance_id TEXT NOT NULL,
                input_id TEXT NOT NULL,
                input_type TEXT NOT NULL,
                PRIMARY KEY (provenance_id, input_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE damage_assessments (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES fire_events(id),
                facility_id TEXT NOT NULL REFERENCES facilities(id),
                fraction_destroyed_low REAL NOT NULL,
                fraction_destroyed_mode REAL NOT NULL,
                fraction_destroyed_high REAL NOT NULL,
                assessed_by TEXT NOT NULL,
                assessment_method TEXT NOT NULL,
                notes TEXT,
                assessed_at TIMESTAMP NOT NULL,
                provenance_id TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE methodology_versions (
                version_id TEXT PRIMARY KEY,
                released_at TIMESTAMP NOT NULL,
                pdf_url TEXT NOT NULL,
                changelog TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE pipeline_runs (
                id TEXT PRIMARY KEY,
                flow_name TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP,
                status TEXT NOT NULL,
                metrics TEXT NOT NULL DEFAULT '{}'
            )
        """))
        conn.execute(text("""
            CREATE TABLE firms_detections (
                id TEXT PRIMARY KEY,
                latitude REAL, longitude REAL,
                brightness REAL, frp REAL,
                confidence TEXT,
                acq_datetime TIMESTAMP, satellite TEXT,
                instrument TEXT, version TEXT,
                bright_t31 REAL, scan REAL, track REAL,
                raw_json TEXT, ingested_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE s2_chips (
                id TEXT PRIMARY KEY,
                event_id TEXT, facility_id TEXT,
                product_id TEXT, acquisition_date TIMESTAMP,
                cloud_cover_pct REAL, storage_path TEXT,
                bands TEXT, fetched_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE acled_events (
                id TEXT PRIMARY KEY,
                acled_id INTEGER UNIQUE,
                event_date DATE, event_type TEXT,
                sub_event_type TEXT, country TEXT,
                admin1 TEXT, admin2 TEXT, location TEXT,
                latitude REAL, longitude REAL,
                source TEXT, notes TEXT,
                raw_json TEXT, ingested_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE validation_reports (
                id TEXT PRIMARY KEY,
                event_id TEXT REFERENCES fire_events(id),
                tropomi_estimate_p50 REAL NOT NULL,
                discrepancy_ratio REAL NOT NULL,
                needs_review INTEGER NOT NULL DEFAULT 0,
                generated_at TIMESTAMP NOT NULL
            )
        """))

    return engine


@pytest.fixture()
def db_session(db_engine):
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def client(db_engine, db_session):
    app = create_app()

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c


NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)

# sa.UUID(as_uuid=True) stores as 32-char hex on non-PG backends.
_FACILITY_UUID = uuid4()
_FACILITY_UUID_2 = uuid4()
_EVENT_UUID = uuid4()
_EVENT_UUID_2 = uuid4()
_PROVENANCE_UUID = uuid4()
_ESTIMATE_UUID = uuid4()

FACILITY_ID = _FACILITY_UUID.hex
FACILITY_ID_2 = _FACILITY_UUID_2.hex
EVENT_ID = _EVENT_UUID.hex
EVENT_ID_2 = _EVENT_UUID_2.hex
PROVENANCE_ID = _PROVENANCE_UUID.hex
ESTIMATE_ID = _ESTIMATE_UUID.hex


@pytest.fixture()
def seed_data(db_session):
    """Insert a minimal set of rows for testing all endpoints."""
    db_session.execute(text(
        "INSERT INTO methodology_versions (version_id, released_at, pdf_url, changelog) "
        "VALUES (:vid, :ra, :url, :cl)"
    ), {"vid": "1.0", "ra": NOW.isoformat(), "url": "/methodology/v1.0.pdf", "cl": "Initial release"})

    db_session.execute(text(
        "INSERT INTO facilities (id, name, facility_type, geometry, country, "
        "capacity_barrels, capacity_uncertainty_pct, operator, source_url, added_at) "
        "VALUES (:id, :name, :ft, :geom, :country, :cap, :unc, :op, :url, :added)"
    ), {
        "id": FACILITY_ID, "name": "Abadan Refinery", "ft": "REFINERY",
        "geom": "POINT(48.28 30.35)", "country": "IRN", "cap": 400000.0,
        "unc": 30.0, "op": "NIOC", "url": "https://example.com/abadan",
        "added": NOW.isoformat(),
    })
    db_session.execute(text(
        "INSERT INTO facilities (id, name, facility_type, geometry, country, "
        "capacity_barrels, capacity_uncertainty_pct, operator, source_url, added_at) "
        "VALUES (:id, :name, :ft, :geom, :country, :cap, :unc, :op, :url, :added)"
    ), {
        "id": FACILITY_ID_2, "name": "Isfahan Depot", "ft": "OIL_DEPOT",
        "geom": "POINT(51.67 32.65)", "country": "IRN", "cap": 100000.0,
        "unc": 25.0, "op": None, "url": "https://example.com/isfahan",
        "added": NOW.isoformat(),
    })

    db_session.execute(text(
        "INSERT INTO provenance_records (id, produced_by, method, parameters, "
        "produced_at, confidence_label) "
        "VALUES (:id, :pb, :m, :p, :pa, :cl)"
    ), {
        "id": PROVENANCE_ID, "pb": "wced.quantify.frp",
        "m": "frp_to_co2_v1.0", "p": "{}",
        "pa": NOW.isoformat(), "cl": "CONFIRMED",
    })

    db_session.execute(text(
        "INSERT INTO fire_events (id, facility_id, detected_at, last_seen_at, "
        "peak_frp_mw, total_frp_integral_mj, detection_source, confidence_label, "
        "status, provenance_id, created_at, updated_at) "
        "VALUES (:id, :fid, :da, :ls, :frp, :tfi, :ds, :cl, :st, :pid, :ca, :ua)"
    ), {
        "id": EVENT_ID, "fid": FACILITY_ID,
        "da": datetime(2026, 3, 5, 8, 0, tzinfo=timezone.utc).isoformat(),
        "ls": datetime(2026, 3, 5, 20, 0, tzinfo=timezone.utc).isoformat(),
        "frp": 450.0, "tfi": 12000.0, "ds": "FIRMS_VIIRS",
        "cl": "CONFIRMED", "st": "PUBLISHED",
        "pid": PROVENANCE_ID,
        "ca": NOW.isoformat(), "ua": NOW.isoformat(),
    })
    db_session.execute(text(
        "INSERT INTO fire_events (id, facility_id, detected_at, last_seen_at, "
        "peak_frp_mw, total_frp_integral_mj, detection_source, confidence_label, "
        "status, provenance_id, created_at, updated_at) "
        "VALUES (:id, :fid, :da, :ls, :frp, :tfi, :ds, :cl, :st, :pid, :ca, :ua)"
    ), {
        "id": EVENT_ID_2, "fid": FACILITY_ID_2,
        "da": datetime(2026, 3, 10, 6, 0, tzinfo=timezone.utc).isoformat(),
        "ls": datetime(2026, 3, 10, 18, 0, tzinfo=timezone.utc).isoformat(),
        "frp": 200.0, "tfi": 5000.0, "ds": "FIRMS_VIIRS",
        "cl": "VERIFIED", "st": "PUBLISHED",
        "pid": PROVENANCE_ID,
        "ca": NOW.isoformat(), "ua": NOW.isoformat(),
    })

    db_session.execute(text(
        "INSERT INTO emission_estimates (id, event_id, methodology_version, method, "
        "p5, p50, p95, samples_ref, units, provenance_id, parameter_versions, created_at) "
        "VALUES (:id, :eid, :mv, :m, :p5, :p50, :p95, :sr, :u, :pid, :pv, :ca)"
    ), {
        "id": ESTIMATE_ID, "eid": EVENT_ID, "mv": "1.0",
        "m": "frp_to_co2_v1.0", "p5": 800.0, "p50": 1200.0, "p95": 1800.0,
        "sr": None, "u": "tCO2e", "pid": PROVENANCE_ID,
        "pv": "{}", "ca": NOW.isoformat(),
    })

    est2_id = uuid4().hex
    db_session.execute(text(
        "INSERT INTO emission_estimates (id, event_id, methodology_version, method, "
        "p5, p50, p95, samples_ref, units, provenance_id, parameter_versions, created_at) "
        "VALUES (:id, :eid, :mv, :m, :p5, :p50, :p95, :sr, :u, :pid, :pv, :ca)"
    ), {
        "id": est2_id, "eid": EVENT_ID_2, "mv": "1.0",
        "m": "frp_to_co2_v1.0", "p5": 300.0, "p50": 500.0, "p95": 800.0,
        "sr": None, "u": "tCO2e", "pid": PROVENANCE_ID,
        "pv": "{}", "ca": NOW.isoformat(),
    })

    db_session.commit()
    return {
        "facility_id": str(_FACILITY_UUID),
        "facility_id_2": str(_FACILITY_UUID_2),
        "event_id": str(_EVENT_UUID),
        "event_id_2": str(_EVENT_UUID_2),
        "provenance_id": str(_PROVENANCE_UUID),
        "estimate_id": str(_ESTIMATE_UUID),
    }
