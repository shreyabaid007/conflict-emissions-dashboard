# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Identity

**Name:** War Carbon Emissions Dashboard (WCED)
**Mission:** A near-real-time, publicly auditable dashboard quantifying CO₂ emissions from oil and fuel infrastructure fires during the 2026 Iran–US–Israel war, using only public satellite data and peer-reviewed emission factors.
**Stage:** V1 live — oil/fuel fire emissions only, methodology v1.0.5 (baseline subtraction + fraction-destroyed recalibration). Editorial workflow active, 7 facilities tracked, 47 events detected. Architecture supports future emission-category modules.

## Core Principles (Non-Negotiable)

1. **Provenance is mandatory.** Every numeric output traces back through a chain of cited sources. No estimate exists without its provenance record.
2. **Uncertainty is mandatory.** Every emission number is a distribution, not a point. We report 5th/50th/95th percentiles.
3. **AI outputs are paired with provenance and passed through uncertainty propagation.** AI is never the final authority on a number.
4. **Triangulation before publication.** No incident is dashboarded until it has ≥2 independent sources OR satellite confirmation.
5. **Version everything.** Every dashboard state is reproducible from a git-tracked snapshot. Revisions are logged publicly.
6. **Be honest about latency.** "Near-real-time, updated daily" — never claim live/real-time when the underlying data has hours-to-days latency.
7. **Open methodology, open data, open code.** CC-BY 4.0 data, MIT code, methodology PDF versioned in repo.
8. **Visibility tool, not accountability tool.** Avoid legal-weaponization framing.

## What This Project Is NOT

- Not a real-time alert system
- Not a tool for predicting future strikes (only post-event reporting)
- Not a tool for attributing emissions to specific belligerents in adversarial contexts
- Not a replacement for peer-reviewed academic publication — it complements it

## Tech Stack

- **Language:** Python 3.11+
- **API framework:** FastAPI
- **Data validation:** Pydantic v2
- **Geospatial:** GeoPandas, Shapely, Rasterio
- **Satellite data:** pystac-client, planetary-computer (Microsoft Planetary Computer)
- **AI/LLM:** Anthropic Claude API (claude-opus-4-7 for complex reasoning, claude-haiku-4-5 for high-volume classification); OpenRouter as alternative provider; structured outputs via Pydantic models
- **Monte Carlo:** NumPy + SciPy
- **Storage:** PostgreSQL + PostGIS via GeoAlchemy2 (events, facilities, estimates)
- **Frontend:** Next.js + MapLibre GL + Tailwind CSS (no proprietary mapping); served separately from API
- **Task runner:** Justfile
- **Observability:** Prometheus + Grafana; OpenTelemetry middleware; structured JSON logs (structlog)
- **Deployment:** Docker Compose for dev; Kubernetes-ready for prod (Helm charts in `deploy/`)
- **HTTP clients:** httpx (async) + tenacity (retry)

## Repository Structure

See `.steering/structure.md` for the full canonical layout. Key directories:
- `wced/` — main Python package
- `wced/ingest/` — data source connectors (firms, acled, gdelt, sentinel2, sentinel5p)
- `wced/detect/` — fire detection (hotspot, facility_match, baseline, persistence)
- `wced/verify/` — verification pipeline (sentinel2_check, acled_corroboration, confidence, editorial)
- `wced/quantify/` — emissions calculations (frp, inventory, factors, aggregate, reconcile, distribution)
- `wced/ai/` — Claude client wrapper + vision classify
- `wced/provenance/` — provenance store
- `wced/pipeline/` — orchestration flows (daily_ingest, quantification, validation_weekly)
- `wced/api/` — FastAPI app with routes (events, facilities, aggregates, timeseries, meta)
- `wced/db/` — SQLAlchemy models, Alembic migrations, repositories
- `wced/cli/` — Typer CLI (`main.py` for pipeline commands, `verify.py` for editorial)
- `data/` — facility registry GeoJSON, emission_factors.yaml, parameter_distributions.yaml; NEVER raw satellite data
- `methodology/` — versioned methodology docs (PDF + LaTeX source + CHANGELOG.md)
- `tests/` — pytest; unit/, integration/, methodology/ suites; aim for ≥80% coverage on `quantify/` and `provenance/`
- `deploy/` — Docker Compose dev stack, Dockerfiles, Grafana/Prometheus config
- `scripts/` — one-off operational scripts (bootstrap_facilities, backfill, approve_top_events)

## Coding Standards

- **Type hints everywhere.** Run mypy in strict mode on `quantify/` and `provenance/`.
- **Pydantic v2 models for all data crossing module boundaries.** No dicts-as-data.
- **Pure functions in `quantify/`.** Side effects only at the edges (ingest, persist, serve).
- **Every emission estimate function returns a Distribution object** (not a float). The Distribution has `.p5`, `.p50`, `.p95`, `.samples`, `.provenance`.
- **No silent failures.** Use Result types or raise; never return None for "I don't know."
- **Logging:** structured (structlog), never print(). Include event_id, facility_id, source, confidence in every log record.
- **Comments explain WHY (the methodology choice), not WHAT.** Link to methodology PDF section.

## How Claude Should Help

When asked to implement a feature:
1. **Check if it touches a quantification step.** If yes, the methodology PDF (`methodology/v1.x.pdf`) is the source of truth. Implementation must match the equation in the methodology. Cite the section in a code comment.
2. **Confirm provenance requirements.** Anything producing a number must accept and propagate a `Provenance` object.
3. **Confirm uncertainty handling.** Any function returning an emission estimate must return a `Distribution`, not a point.
4. **Suggest tests first.** For `quantify/` code, write the test (with hand-computed expected values from the methodology PDF) before the implementation.
5. **Default to small modules.** A connector for one data source is its own module. Don't combine FIRMS and Sentinel into one file.
6. **Ask before adding dependencies.** We minimize the dependency surface — every new library is a security and provenance liability.

## Anti-Patterns to Avoid

- Adding "real-time" to anything in user-facing copy
- Claiming certainty in emission estimates (always bounds)
- Letting LLM outputs reach the database without a Provenance record
- Mixing facility metadata with raw satellite data
- Implementing methodology before it's written in the methodology PDF
- Using floating-point comparisons without explicit tolerances in tests
- Caching across methodology versions (cache keys must include methodology version)
- Hard-coding emission factors in Python (they live in `data/emission_factors.yaml`)
- Auto-publishing incidents to the dashboard without editorial review for the first 6 months
- Scalar arithmetic on `Distribution` that silently inherits the parent's `provenance_id` — the scalar's own source must be recorded; use `apply_scalar(factor, provenance_id=factor_record_id)` when the scalar comes from `data/emission_factors.yaml`

## Sensitive Areas — Require Extra Care

- **Anything involving attribution** to a specific belligerent → leave aggregated unless explicitly required
- **Anything involving Iranian, Israeli, US, or Gulf official statements** → store as `Claimed` confidence, never `Confirmed`
- **Anything involving facility coordinates with sub-100m precision** → check dual-use review checklist
- **Anything involving casualty figures** → out of scope for this project; do not store

## Methodology Versioning

- Methodology is versioned with semantic versioning (v1.0, v1.0.1, v1.0.2, v1.1, v2.0)
- Database stores `methodology_version` on every estimate
- Recomputing all estimates is a deliberate operation via `wced recompute --methodology-version <ver>`, never automatic
- The methodology PDF must be approved by the Scientific Steering Committee before being released as a version
- Current versions: v1.0 (raw FRP), v1.0.1 (baseline subtraction), v1.0.2 (pre-war baseline data fix), v1.0.5 (fraction-destroyed recalibration for storage-type facilities)
- **Latest live version: v1.0.5**
- See `methodology/CHANGELOG.md` for detailed version history

## Editorial Workflow

- State machine: `PENDING_REVIEW` → `PUBLISHED` (approve) or `REJECTED` (reject)
- `REJECTED` → `PENDING_REVIEW` (resubmit); `PUBLISHED` → `RETRACTED` (retract)
- Detection runs with `--no-auto-publish` (enforced in Justfile `detect` recipe)
- `wced verify approve/reject/resubmit/retract` commands in `cli/verify.py`
- `wced verify add-assessment` attaches DamageAssessment to already-published events
- Never silently delete; always changelog

## Useful Documents

- `HANDOFF.md` — current state snapshot (numbers, what's live, what's pending)
- `docs/V1_PLAN.md` — V1 scope, methodology, and current status
- `docs/DEV_SETUP.md` — developer setup guide
- `docs/RUNBOOK.md` — operational runbook for running the pipeline
- `docs/INCIDENT_RESPONSE.md` — error handling and retractions
- `docs/LAUNCH_CHECKLIST.md` — pre-launch verification
- `.steering/product.md` — product vision and user personas
- `.steering/structure.md` — repo layout (canonical)
- `.steering/tech.md` — tech stack rationale
- `methodology/v1.0.pdf` — source of truth for base equations (frozen)
- `methodology/v1.0.5.tex` — current methodology (supersedes v1.0)
- `methodology/CHANGELOG.md` — methodology version history (v1.0 through v1.0.5)

## Operations

- **Dev stack:** `docker compose -f deploy/docker-compose.yml up` (or `just up`)
- **Task runner:** Justfile wraps common operations (`just detect`, `just quantify`, `just verify`)
- **CLI entry point:** `wced` (Typer CLI in `wced/cli/main.py`)
- **Key CLI commands:** `wced detect`, `wced quantify`, `wced recompute`, `wced ingest firms-historical`, `wced verify approve/reject/add-assessment`
- **DB port:** 5432 by default in dev docker-compose (override with `POSTGRES_PORT` env var)
- **FIRMS archival ingest:** `wced ingest firms-historical --start YYYY-MM-DD --end YYYY-MM-DD` uses SP sources with 5-day API chunks

## When Stuck

If methodology is unclear: stop, ask. Don't invent.
If a source seems unreliable: stop, flag for editorial review.
If an AI output disagrees with a deterministic calculation: trust the deterministic one.
If uncertainty bounds seem implausibly narrow: they probably are; revisit the parameter PDFs.