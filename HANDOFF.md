# WCED Handoff — Current State Snapshot

**Date:** 2026-05-24
**Methodology version:** v1.0.5

## Numbers

- **Cumulative Iranian p50:** 75,619 tCO₂e
- **Events detected:** 47
- **Events with emission estimates:** 27
- **Fully reconciled (FRP vs. inventory):** 2
- **Flagged needs_review:** 0
- **Iranian facilities tracked:** 7

## What's Live

- Detection pipeline: FIRMS ingest → hotspot clustering → facility match → baseline subtraction → persistence filter
- FRP-based quantification with 10,000-sample Monte Carlo
- Inventory-based quantification for facilities with capacity data and damage assessments
- FRP/inventory reconciliation with envelope distribution
- ACLED corroboration + confidence label assignment (CONFIRMED/VERIFIED/REPORTED/SUSPECTED/CLAIMED)
- Editorial workflow: `PENDING_REVIEW → PUBLISHED` with `--no-auto-publish` enforced
- Full provenance chain from satellite observation to emission estimate
- FastAPI with routes: /events, /facilities, /aggregates, /timeseries, /meta
- Next.js frontend: map view, event detail, emission timeline, cumulative chart, methodology page
- Docker Compose dev stack: Postgres+PostGIS, Redis, MinIO, Prefect, Grafana, Prometheus
- Pre-war FIRMS baselines: 12 months (2025-02-28 to 2026-02-27) ingested via `firms-historical`
- Methodology PDF (v1.0) + CHANGELOG with 6 versions (v1.0 through v1.0.5)

## What's Pending

- **Facility registry expansion:** Cross-check against CEOBS incident database for missing facilities
- **Substack article:** Draft and publish the methodology explainer for non-technical audiences
- **GitHub public release:** Finalize README, license files, remove any hardcoded dev credentials
- **Sentinel-2 optical confirmation:** Classifier exists (`wced/ai/classify.py`) but not integrated into the pipeline
- **TROPOMI top-down validation:** Module exists (`wced/validate/tropomi.py`) but weekly batch not yet scheduled
- **GDELT integration:** Connector exists (`wced/ingest/gdelt.py`) but not used in verification pipeline

## Key Files

| File | Purpose |
|------|---------|
| `methodology/v1.0.pdf` | Source of truth for equations (v1.0 base) |
| `methodology/CHANGELOG.md` | All version diffs from v1.0 to v1.0.5 |
| `data/emission_factors.yaml` | All emission factors with citations |
| `data/parameter_distributions.yaml` | Monte Carlo parameter PDFs |
| `data/facilities/iran_oil_gas.geojson` | Curated facility registry (7 facilities) |
| `Justfile` | Task runner for all pipeline operations |
| `docs/DEV_SETUP.md` | Developer setup guide |
| `docs/RUNBOOK.md` | Operational runbook for running the pipeline |

## Architecture

```
FIRMS API → ingest/firms.py → detect/hotspot.py → detect/facility_match.py
  → detect/baseline.py → detect/persistence.py → verify/confidence.py
  → verify/editorial.py (PENDING_REVIEW) → [human approve]
  → quantify/frp.py + quantify/inventory.py → quantify/reconcile.py
  → api/routes/ → frontend
```

Provenance is recorded at every step. Every emission number is a Distribution (p5/p50/p95 + samples), never a scalar.
