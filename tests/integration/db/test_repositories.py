"""Integration tests for database repositories against a real PostgreSQL instance.

These tests require Docker to be running. They exercise the full stack:
SQLAlchemy models -> repositories -> PostGIS database.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from wced.db.repositories.facility import PostgisFacilityRepository
from wced.db.repositories.fire_event import FireEventRepository
from wced.db.repositories.emission import EmissionEstimateRepository
from wced.db.repositories.provenance import ProvenanceRepository
from wced.db.repositories.damage import DamageAssessmentRepository
from wced.db.repositories.editorial import EditorialActionRepository
from wced.db.repositories.pipeline import PipelineRunRepository
from wced.db.repositories.ingestion import FirmsDetectionRepository, AcledEventRepository, S2ChipRepository
from wced.db.repositories.validation import ValidationReportRepository
from wced.models.facility import Facility, FacilityType

pytestmark = pytest.mark.integration

NOW = datetime.now(timezone.utc)


def _make_facility(**overrides):
    defaults = dict(
        id=uuid4(),
        name="Test Refinery",
        facility_type=FacilityType.REFINERY,
        geometry_wkt="POINT(51.4 35.7)",
        country="IRN",
        capacity_barrels=100_000.0,
        capacity_uncertainty_pct=30.0,
        operator="TestOp",
        source_url="https://example.com/facility",
        added_at=NOW,
        notes=None,
    )
    defaults.update(overrides)
    return Facility(**defaults)


class TestPostgisFacilityRepository:
    def test_upsert_and_get(self, db_session):
        repo = PostgisFacilityRepository(db_session)
        facility = _make_facility()

        repo.upsert(facility)
        retrieved = repo.get(facility.id)

        assert retrieved.id == facility.id
        assert retrieved.name == "Test Refinery"
        assert retrieved.facility_type == FacilityType.REFINERY
        assert retrieved.country == "IRN"

    def test_upsert_idempotent(self, db_session):
        repo = PostgisFacilityRepository(db_session)
        facility = _make_facility()

        repo.upsert(facility)
        updated = _make_facility(id=facility.id, name="Updated Refinery")
        repo.upsert(updated)

        retrieved = repo.get(facility.id)
        assert retrieved.name == "Updated Refinery"
        assert len(repo) == 1

    def test_get_missing_raises(self, db_session):
        repo = PostgisFacilityRepository(db_session)
        with pytest.raises(KeyError):
            repo.get(uuid4())

    def test_iter_by_country(self, db_session):
        repo = PostgisFacilityRepository(db_session)
        repo.upsert(_make_facility(id=uuid4(), country="IRN"))
        repo.upsert(_make_facility(id=uuid4(), country="ISR"))

        irn = list(repo.iter_by_country("IRN"))
        assert len(irn) == 1
        assert irn[0].country == "IRN"

    def test_len(self, db_session):
        repo = PostgisFacilityRepository(db_session)
        assert len(repo) == 0
        repo.upsert(_make_facility())
        assert len(repo) == 1


class TestFireEventRepository:
    def _insert_facility(self, db_session):
        repo = PostgisFacilityRepository(db_session)
        f = _make_facility()
        repo.upsert(f)
        return f.id

    def test_insert_and_get(self, db_session):
        fac_id = self._insert_facility(db_session)
        repo = FireEventRepository(db_session)
        event_id = uuid4()

        repo.insert(
            id=event_id, facility_id=fac_id,
            detected_at=NOW, last_seen_at=NOW,
            peak_frp_mw=150.0, total_frp_integral_mj=None,
            detection_source="FIRMS_VIIRS",
            confidence_label="CONFIRMED",
            status="PENDING_REVIEW",
            provenance_id=uuid4(),
            created_at=NOW, updated_at=NOW,
        )

        row = repo.get(event_id)
        assert row is not None
        assert row["id"] == event_id
        assert row["peak_frp_mw"] == 150.0

    def test_update_status(self, db_session):
        fac_id = self._insert_facility(db_session)
        repo = FireEventRepository(db_session)
        event_id = uuid4()
        repo.insert(
            id=event_id, facility_id=fac_id,
            detected_at=NOW, last_seen_at=NOW,
            peak_frp_mw=100.0, total_frp_integral_mj=None,
            detection_source="FIRMS_VIIRS",
            confidence_label="REPORTED",
            status="PENDING_REVIEW",
            provenance_id=uuid4(),
            created_at=NOW, updated_at=NOW,
        )

        repo.update_status(event_id, "PUBLISHED", NOW)
        row = repo.get(event_id)
        assert row["status"] == "PUBLISHED"

    def test_list_by_status(self, db_session):
        fac_id = self._insert_facility(db_session)
        repo = FireEventRepository(db_session)
        for _ in range(3):
            repo.insert(
                id=uuid4(), facility_id=fac_id,
                detected_at=NOW, last_seen_at=NOW,
                peak_frp_mw=50.0, total_frp_integral_mj=None,
                detection_source="FIRMS_VIIRS",
                confidence_label="REPORTED",
                status="PENDING_REVIEW",
                provenance_id=uuid4(),
                created_at=NOW, updated_at=NOW,
            )
        assert len(repo.list_by_status("PENDING_REVIEW")) == 3
        assert len(repo.list_by_status("PUBLISHED")) == 0

    def test_count(self, db_session):
        fac_id = self._insert_facility(db_session)
        repo = FireEventRepository(db_session)
        assert repo.count() == 0
        repo.insert(
            id=uuid4(), facility_id=fac_id,
            detected_at=NOW, last_seen_at=NOW,
            peak_frp_mw=50.0, total_frp_integral_mj=None,
            detection_source="FIRMS_VIIRS",
            confidence_label="REPORTED",
            status="PENDING_REVIEW",
            provenance_id=uuid4(),
            created_at=NOW, updated_at=NOW,
        )
        assert repo.count() == 1


class TestEmissionEstimateRepository:
    def _setup_event(self, db_session):
        fac_repo = PostgisFacilityRepository(db_session)
        f = _make_facility()
        fac_repo.upsert(f)
        ev_repo = FireEventRepository(db_session)
        eid = uuid4()
        ev_repo.insert(
            id=eid, facility_id=f.id,
            detected_at=NOW, last_seen_at=NOW,
            peak_frp_mw=100.0, total_frp_integral_mj=None,
            detection_source="FIRMS_VIIRS",
            confidence_label="CONFIRMED",
            status="PUBLISHED",
            provenance_id=uuid4(),
            created_at=NOW, updated_at=NOW,
        )
        return eid

    def test_insert_and_get(self, db_session):
        event_id = self._setup_event(db_session)
        repo = EmissionEstimateRepository(db_session)
        est_id = uuid4()

        repo.insert(
            id=est_id, event_id=event_id,
            methodology_version="1.0", method="FRP",
            p5=10.0, p50=50.0, p95=120.0,
            samples_ref="s3://bucket/samples.npy",
            units="tCO2e", provenance_id=uuid4(),
            parameter_versions={"emission_factors": "abc123"},
            created_at=NOW,
        )

        row = repo.get(est_id)
        assert row is not None
        assert row["p50"] == 50.0
        assert row["method"] == "FRP"

    def test_list_by_event(self, db_session):
        event_id = self._setup_event(db_session)
        repo = EmissionEstimateRepository(db_session)
        for method in ("FRP", "INV", "RECONC"):
            repo.insert(
                id=uuid4(), event_id=event_id,
                methodology_version="1.0", method=method,
                p5=5.0, p50=25.0, p95=60.0,
                samples_ref=None, units="tCO2e",
                provenance_id=uuid4(),
                parameter_versions={},
                created_at=NOW,
            )
        assert len(repo.list_by_event(event_id)) == 3


class TestProvenanceRepository:
    def test_insert_record_and_source_with_link(self, db_session):
        repo = ProvenanceRepository(db_session)
        src_id = uuid4()
        repo.insert_source(
            id=src_id, source_type="SATELLITE",
            identifier="S2A_MSIL2A_20260301",
            retrieved_at=NOW, content_hash="abc123",
        )

        rec_id = uuid4()
        repo.insert_record(
            id=rec_id, produced_by="wced.quantify.frp",
            method="frp_to_co2_v1.0",
            parameters={"combustion_factor": 0.0031},
            produced_at=NOW, confidence_label="CONFIRMED",
        )

        repo.link_input(rec_id, src_id, input_type="source")

        record = repo.get_record(rec_id)
        assert record is not None
        assert record["produced_by"] == "wced.quantify.frp"

        inputs = repo.get_inputs(rec_id)
        assert len(inputs) == 1
        assert inputs[0]["input_id"] == src_id


class TestDamageAssessmentRepository:
    def _setup_event(self, db_session):
        fac_repo = PostgisFacilityRepository(db_session)
        f = _make_facility()
        fac_repo.upsert(f)
        ev_repo = FireEventRepository(db_session)
        eid = uuid4()
        ev_repo.insert(
            id=eid, facility_id=f.id,
            detected_at=NOW, last_seen_at=NOW,
            peak_frp_mw=100.0, total_frp_integral_mj=None,
            detection_source="FIRMS_VIIRS",
            confidence_label="CONFIRMED",
            status="PUBLISHED",
            provenance_id=uuid4(),
            created_at=NOW, updated_at=NOW,
        )
        return eid, f.id

    def test_insert_and_list(self, db_session):
        event_id, fac_id = self._setup_event(db_session)
        repo = DamageAssessmentRepository(db_session)

        repo.insert(
            id=uuid4(), event_id=event_id, facility_id=fac_id,
            fraction_destroyed_low=0.1,
            fraction_destroyed_mode=0.3,
            fraction_destroyed_high=0.5,
            assessed_by="analyst:test",
            assessment_method="SENTINEL2_VISUAL",
            notes="Visible damage in SWIR",
            assessed_at=NOW, provenance_id=uuid4(),
        )

        rows = repo.list_by_event(event_id)
        assert len(rows) == 1
        assert rows[0]["fraction_destroyed_mode"] == 0.3


class TestEditorialActionRepository:
    def _setup_event(self, db_session):
        fac_repo = PostgisFacilityRepository(db_session)
        f = _make_facility()
        fac_repo.upsert(f)
        ev_repo = FireEventRepository(db_session)
        eid = uuid4()
        ev_repo.insert(
            id=eid, facility_id=f.id,
            detected_at=NOW, last_seen_at=NOW,
            peak_frp_mw=100.0, total_frp_integral_mj=None,
            detection_source="FIRMS_VIIRS",
            confidence_label="CONFIRMED",
            status="PENDING_REVIEW",
            provenance_id=uuid4(),
            created_at=NOW, updated_at=NOW,
        )
        return eid

    def test_insert_and_list(self, db_session):
        event_id = self._setup_event(db_session)
        repo = EditorialActionRepository(db_session)

        repo.insert(
            id=uuid4(), event_id=event_id,
            action_type="APPROVED", reviewer="analyst:test",
            notes=None,
            previous_status="PENDING_REVIEW",
            new_status="PUBLISHED",
            acted_at=NOW,
        )

        actions = repo.list_by_event(event_id)
        assert len(actions) == 1
        assert actions[0]["action_type"] == "APPROVED"


class TestPipelineRunRepository:
    def test_insert_and_finish(self, db_session):
        repo = PipelineRunRepository(db_session)
        run_id = uuid4()

        repo.insert(
            id=run_id, flow_name="daily_ingest",
            started_at=NOW, status="RUNNING",
        )

        run = repo.get(run_id)
        assert run["status"] == "RUNNING"
        assert run["ended_at"] is None

        repo.finish(
            run_id, status="COMPLETED", ended_at=NOW,
            metrics={"events_processed": 42},
        )

        run = repo.get(run_id)
        assert run["status"] == "COMPLETED"
        assert run["metrics"]["events_processed"] == 42

    def test_list_recent(self, db_session):
        repo = PipelineRunRepository(db_session)
        for i in range(3):
            repo.insert(
                id=uuid4(), flow_name="daily_ingest",
                started_at=NOW, status="COMPLETED",
            )
        repo.insert(
            id=uuid4(), flow_name="weekly_validation",
            started_at=NOW, status="COMPLETED",
        )

        assert len(repo.list_recent()) == 4
        assert len(repo.list_recent("daily_ingest")) == 3


class TestFirmsDetectionRepository:
    def test_insert_batch_and_count(self, db_session):
        repo = FirmsDetectionRepository(db_session)
        rows = [
            dict(
                id=uuid4(), latitude=35.7, longitude=51.4,
                brightness=320.0, frp=45.0, confidence="high",
                acq_datetime=NOW, satellite="N20",
                instrument="VIIRS", version="2.0",
                bright_t31=290.0, scan=0.4, track=0.5,
                raw_json=None, ingested_at=NOW,
            )
            for _ in range(5)
        ]
        assert repo.insert_batch(rows) == 5
        assert repo.count() == 5


class TestValidationReportRepository:
    def _setup_event(self, db_session):
        fac_repo = PostgisFacilityRepository(db_session)
        f = _make_facility()
        fac_repo.upsert(f)
        ev_repo = FireEventRepository(db_session)
        eid = uuid4()
        ev_repo.insert(
            id=eid, facility_id=f.id,
            detected_at=NOW, last_seen_at=NOW,
            peak_frp_mw=100.0, total_frp_integral_mj=None,
            detection_source="FIRMS_VIIRS",
            confidence_label="CONFIRMED",
            status="PUBLISHED",
            provenance_id=uuid4(),
            created_at=NOW, updated_at=NOW,
        )
        return eid

    def test_insert_and_list_needing_review(self, db_session):
        event_id = self._setup_event(db_session)
        repo = ValidationReportRepository(db_session)

        repo.insert(
            id=uuid4(), event_id=event_id,
            tropomi_estimate_p50=120.0,
            discrepancy_ratio=2.5,
            needs_review=True,
            generated_at=NOW,
        )

        needing = repo.list_needing_review()
        assert len(needing) == 1
        assert needing[0]["discrepancy_ratio"] == 2.5
