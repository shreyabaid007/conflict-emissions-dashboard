# War Carbon Emissions Dashboard (WCED)

A near-real-time, publicly auditable dashboard quantifying CO2 emissions from
oil and fuel infrastructure fires during the 2026 Iran-US-Israel war, using
only public satellite data and peer-reviewed emission factors.

> **This is an academic research tool.** It is not a legal accountability
> instrument, not a real-time alert system, and not a tool for predicting future
> events. Attribution of responsibility to specific belligerents is out of scope.
> All emission estimates are distributions with explicit uncertainty bounds
> (5th/50th/95th percentile). No incident appears on the dashboard until it
> passes the editorial workflow. Methodology is versioned; numbers are
> reproducible from a git-tracked snapshot.

## Current Status (V1 — paused)

**Scope:** Oil and fuel infrastructure fire emissions in Iran and the Gulf
region, 28 Feb 2026 - present. Methodology version: **v1.1.0**.

**Headline (as of 2026-05-24):** ~75.6 kt CO2e cumulative (p50) across
7 Iranian facilities, based on 47 detected fire events (27 with emission
estimates, 2 fully reconciled FRP vs. inventory).

This project is paused at V1. The codebase is open-sourced as-is for
transparency and reproducibility. See [Status / Roadmap](#status--roadmap) for
what works and what remains.

## What Works

- **FIRMS ingestion:** NASA FIRMS / VIIRS fire radiative power data, with
  12-month pre-war baseline (2025-02-28 to 2026-02-27) and archival ingest
- **Facility matching:** Curated GeoJSON registry of 7 Iranian oil/gas
  facilities (GEM / OSM sourced), with hotspot-to-facility spatial matching
- **Dual emission estimates:** FRP-based (bottom-up from satellite fire power)
  and inventory-based (top-down from facility capacity + damage fraction), with
  envelope reconciliation when both are available
- **Monte Carlo uncertainty:** 10,000-sample distributions for every estimate,
  reporting p5/p50/p95 percentiles. Parameter PDFs in
  `data/parameter_distributions.yaml`
- **Provenance records:** Every numeric output traces back through a chain of
  cited sources — no estimate exists without its provenance record
- **Methodology versioning:** v1.0 through v1.1.0, with `methodology_version`
  stored on every estimate. Recomputation is a deliberate operation
  (`wced recompute`), never automatic
- **FastAPI read-only API** under `/v1`: events, facilities, aggregates,
  timeseries, meta, provenance, revisions routes
- **MapLibre frontend:** Next.js + MapLibre GL + Tailwind CSS dashboard with
  map view, event detail, emission timeline, and cumulative chart
- **Editorial state machine:** `PENDING_REVIEW -> PUBLISHED -> RETRACTED` with
  approve/reject/resubmit/retract commands, append-only `publication_log`
- **Confidence-gated publish policy:** Only `Confirmed`/`Verified` events
  auto-publish; confidence, provenance, distribution, and cross-method gates
  enforced in code
- **Emission-category plugin system:** Protocol-based plugin architecture
  (`wced/categories/base.py`) with entry-point discovery; `oil_fuel_fire` is
  the first and currently only plugin

## Known Limitations

**All events are currently labelled `REPORTED`.** The verification /
corroboration layer — GDELT conflict-event matching, Sentinel-2 SWIR fire
confirmation, and confidence promotion to `VERIFIED`/`CONFIRMED` — is
implemented in code but **not yet wired into the data pipeline**. The detection
path (`wced detect` / backfill) hardcodes `REPORTED` and never calls the
verification stages. The `daily_ingest` flow that does call verification writes
to in-memory stores, not the database.

Headline numbers are FRP/inventory estimates with full uncertainty bounds,
pending the verification layer. This is an honest data limitation, not a
methodology error. See [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)
for the full technical explanation.

Other limitations:

- GDELT DOC API returns no geocoded coordinates, so spatial corroboration via
  the current connector is structurally impossible. A switch to GDELT GEO 2.0
  or Events 2.0 flat-files is needed
- Sentinel-2 chips exist for 5 events but have no classification result stored;
  the AI classifier exists but its output is not persisted
- Only one emission category (oil/fuel fires) is implemented; the plugin system
  supports more but none are built
- The project tracks 7 facilities; cross-checking against CEOBS for
  completeness is pending

## Architecture

```
NASA FIRMS ──► Ingest ──► Detect ──► Quantify ──► API ──► Frontend
  (VIIRS)      (firms)    (hotspot    (frp,        (FastAPI) (Next.js +
                           cluster,    inventory,             MapLibre)
                           facility    reconcile,
                           match,      distribution)
                           baseline)
                              │
                              ▼
                          Verify [not yet wired]
                          (GDELT corroboration,
                           S2 SWIR confirmation,
                           confidence assignment)
```

Three parallel estimation methods:
- **FRP-based (bottom-up):** VIIRS fire radiative power -> combustion rate ->
  CO2, with pre-war baseline subtraction
- **Inventory-based (top-down):** Facility capacity x damage fraction x
  emission factor, for facilities with known capacity
- **Reconciliation:** When both are available, an envelope distribution captures
  the range

Every estimate is a `Distribution` object (not a float) with `.p5`, `.p50`,
`.p95`, `.samples`, `.provenance`.

### Emission-Category Plugins

The system is designed for multiple emission categories via a protocol in
`wced/categories/base.py`, discovered through `pyproject.toml` entry-points
(`wced.categories`). Currently only `oil_fuel_fire` is implemented. Adding a
new category (e.g., embodied carbon from structural damage) means implementing
the protocol and registering the entry-point.

## Data Sources

| Source | Purpose | License |
|--------|---------|---------|
| NASA FIRMS / VIIRS | Fire radiative power (FRP) hotspot detection | Public domain |
| Sentinel-2 (Planetary Computer) | Optical damage confirmation (chips stored, not yet classified in pipeline) | Copernicus / ESA |
| GDELT | Conflict event corroboration (connector built, not yet wired) | Open |
| GEM / OpenStreetMap | Facility geometries | ODbL |

Emission factors: [`data/emission_factors.yaml`](data/emission_factors.yaml)
(cited, never hard-coded in Python).
Parameter distributions: [`data/parameter_distributions.yaml`](data/parameter_distributions.yaml).

## Quick Start

```bash
# Prerequisites: Python 3.11+, Docker, uv, just
curl -LsSf https://astral.sh/uv/install.sh | sh
brew install just  # or cargo install just

# Clone and set up
git clone https://github.com/shreyabaid007/conflict-emission-tracker
cd conflict-emission-tracker
uv sync
cp .env.example .env  # fill in API keys (at minimum: FIRMS_MAP_KEY)

# Start the dev stack (Postgres+PostGIS, Redis, MinIO, Prefect, Grafana)
just bootstrap

# Load facility registry
just facility-load

# Ingest FIRMS data and run detection
just ingest
just detect

# Run the CLI
uv run wced --help

# Tests
just test
```

See [`docs/DEV_SETUP.md`](docs/DEV_SETUP.md) for detailed setup instructions
and [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for operational procedures.

## Methodology

Current methodology version: **v1.1.0**
([CHANGELOG](methodology/CHANGELOG.md)).

- [`methodology/v1.0.pdf`](methodology/v1.0.pdf) — base equations (frozen)
- [`methodology/v1.0.5.pdf`](methodology/v1.0.5.pdf) — baseline subtraction,
  fraction-destroyed recalibration
- v1.1.0 — ACLED-to-GDELT source swap, source-agnostic confidence table
  (documented in CHANGELOG; ACLED retained behind `WCED_ENABLE_ACLED` feature
  flag)

Version history: v1.0 (raw FRP) -> v1.0.1 (baseline subtraction) -> v1.0.2
(pre-war baseline data fix) -> v1.0.5 (fraction-destroyed recalibration) ->
v1.1.0 (GDELT primary, source-agnostic confidence).

## Status / Roadmap

### Done (V1)

- [x] FIRMS ingestion with 12-month pre-war baseline
- [x] Hotspot clustering and facility matching
- [x] FRP-based and inventory-based emission quantification
- [x] Monte Carlo uncertainty propagation (10k samples)
- [x] Full provenance chain
- [x] Methodology v1.0 through v1.1.0
- [x] FastAPI read-only API
- [x] Next.js + MapLibre frontend
- [x] Editorial state machine with publication log
- [x] Confidence-gated auto-publish policy (code-level gates)
- [x] Emission-category plugin architecture

### Future Work (not started or partially built)

- [ ] Wire verification layer into the data pipeline (GDELT corroboration +
      Sentinel-2 SWIR confirmation -> confidence promotion)
- [ ] Switch GDELT connector to GEO 2.0 API or Events 2.0 flat-files for
      geocoded corroboration
- [ ] Persist S2 classification results (classifier exists, persistence does
      not)
- [ ] Second emission category (e.g., embodied carbon from structural damage)
- [ ] TROPOMI / Sentinel-5P top-down CO2 column validation (module exists,
      scheduling pending)
- [ ] Facility registry expansion via CEOBS cross-check
- [ ] `daily_ingest` flow: switch from in-memory stores to DB-backed
      persistence

## How to Cite

> Baid, S. (2026). *War Carbon Emissions Dashboard (WCED): Satellite-based CO2
> emission estimates from oil infrastructure fires during the 2026
> Iran-US-Israel conflict.* v1.1.0.
> https://github.com/shreyabaid007/conflict-emission-tracker

## License

Code: MIT — see [LICENSE](LICENSE)
Data outputs: CC-BY 4.0 — see [DATA_LICENSE](DATA_LICENSE)
