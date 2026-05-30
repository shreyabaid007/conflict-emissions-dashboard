"""Backfill provenance records for existing emission estimates.

Reads all emission_estimates rows and creates the corresponding provenance_records,
sources, and provenance_inputs rows in the DB. This is a one-time migration for
estimates that were computed before provenance persistence was added to recompute.

Usage:
    WCED_DB_DSN=postgresql+psycopg2://wced:wced@localhost:5433/wced \
        python scripts/backfill_provenance.py [--yes]
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from wced.db import models
from wced.db.repositories import ProvenanceRepository
from wced.db.session import get_engine, get_session_factory


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill provenance for existing estimates")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    engine = get_engine()
    Session = get_session_factory(engine)

    with Session() as session:
        # Check current state.
        n_estimates = session.execute(
            select(func.count()).select_from(models.emission_estimates)
        ).scalar_one()
        n_prov = session.execute(
            select(func.count()).select_from(models.provenance_records)
        ).scalar_one()
        n_sources = session.execute(
            select(func.count()).select_from(models.sources)
        ).scalar_one()

        log.info("Current state: %d estimates, %d provenance records, %d sources",
                 n_estimates, n_prov, n_sources)

        if n_prov > 0:
            log.info("Provenance records already exist. Skipping already-backfilled records.")

        # Load all estimates with their events and facilities.
        ee = models.emission_estimates
        fe = models.fire_events
        fa = models.facilities

        rows = session.execute(
            select(
                ee.c.id.label("estimate_id"),
                ee.c.event_id,
                ee.c.provenance_id.label("estimate_prov_id"),
                ee.c.methodology_version,
                ee.c.method,
                ee.c.p50,
                ee.c.created_at.label("estimate_created_at"),
                fe.c.provenance_id.label("event_prov_id"),
                fe.c.detected_at,
                fe.c.detection_source,
                fe.c.confidence_label,
                fe.c.facility_id,
                fa.c.name.label("facility_name"),
            )
            .join(fe, ee.c.event_id == fe.c.id)
            .join(fa, fe.c.facility_id == fa.c.id)
            .order_by(ee.c.created_at.desc())
        ).all()

        # De-duplicate: keep only the latest estimate per event.
        seen_events: set[UUID] = set()
        estimates = []
        for r in rows:
            d = r._asdict()
            if d["event_id"] not in seen_events:
                seen_events.add(d["event_id"])
                estimates.append(d)

        log.info("Will backfill provenance for %d events (latest estimate each)", len(estimates))
        if not args.yes:
            resp = input("Proceed? [y/N] ")
            if resp.strip().lower() != "y":
                print("Aborted.")
                return

        prov_repo = ProvenanceRepository(session)
        now = datetime.now(tz=UTC)
        firms_source_cache: dict[UUID, UUID] = {}
        n_created_sources = 0
        n_created_records = 0
        n_created_links = 0

        for est in estimates:
            fid = est["facility_id"]
            event_prov_id = est["event_prov_id"]
            estimate_prov_id = est["estimate_prov_id"]
            confidence = est["confidence_label"]

            # 1. FIRMS source (one per facility).
            if fid not in firms_source_cache:
                firms_src_id = uuid4()
                try:
                    prov_repo.insert_source(
                        id=firms_src_id,
                        source_type="SATELLITE",
                        identifier=f"NASA FIRMS detections for facility {est['facility_name']}",
                        retrieved_at=now,
                        content_hash=str(fid),
                        metadata={"facility_id": str(fid), "facility_name": est["facility_name"]},
                    )
                    n_created_sources += 1
                except Exception:
                    session.rollback()
                    # Source may already exist from a partial run.
                    firms_src_id = uuid4()
                    prov_repo.insert_source(
                        id=firms_src_id,
                        source_type="SATELLITE",
                        identifier=f"NASA FIRMS detections for facility {est['facility_name']}",
                        retrieved_at=now,
                        content_hash=str(fid) + "_retry",
                        metadata={"facility_id": str(fid), "facility_name": est["facility_name"]},
                    )
                    n_created_sources += 1
                firms_source_cache[fid] = firms_src_id
            firms_src_id = firms_source_cache[fid]

            # 2. Detection-level provenance (fire_event's provenance_id).
            if prov_repo.get_record(event_prov_id) is None:
                prov_repo.insert_record(
                    id=event_prov_id,
                    produced_by="wced.detect.hotspot",
                    method="firms_clustering_v1",
                    parameters={"detection_source": est["detection_source"]},
                    produced_at=est["detected_at"],
                    confidence_label=confidence,
                    notes=None,
                )
                prov_repo.link_input(event_prov_id, firms_src_id, "source")
                n_created_records += 1
                n_created_links += 1

            # 3. Baseline provenance record.
            # We don't have the original baseline provenance_id, so we create
            # a synthetic one derived from the facility ID.
            from uuid import uuid5
            _BASELINE_NS = UUID("a1b2c3d4-0000-5000-8000-000000000001")
            baseline_prov_id = uuid5(_BASELINE_NS, str(fid))
            if prov_repo.get_record(baseline_prov_id) is None:
                prov_repo.insert_record(
                    id=baseline_prov_id,
                    produced_by="wced.detect.baseline",
                    method="rolling_p75_baseline_v1.0.1",
                    parameters={"facility_id": str(fid)},
                    produced_at=est["detected_at"],
                    confidence_label=confidence,
                    notes=None,
                )
                prov_repo.link_input(baseline_prov_id, firms_src_id, "source")
                n_created_records += 1
                n_created_links += 1

            # 4. FRP computation record (estimate's provenance_id).
            if prov_repo.get_record(estimate_prov_id) is None:
                prov_repo.insert_record(
                    id=estimate_prov_id,
                    produced_by="wced.quantify.frp",
                    method=f"frp_to_co2_v{est['methodology_version']}",
                    parameters={
                        "n_samples": 10_000,
                        "rng_seed": 42,
                        "methodology_version": est["methodology_version"],
                    },
                    produced_at=est["estimate_created_at"] or now,
                    confidence_label=confidence,
                    notes=None,
                )
                prov_repo.link_input(estimate_prov_id, event_prov_id, "provenance_record")
                prov_repo.link_input(estimate_prov_id, baseline_prov_id, "provenance_record")
                prov_repo.link_input(estimate_prov_id, firms_src_id, "source")
                n_created_records += 1
                n_created_links += 3

        session.commit()
        log.info(
            "Done. Created %d sources, %d provenance records, %d input links.",
            n_created_sources, n_created_records, n_created_links,
        )


if __name__ == "__main__":
    main()
