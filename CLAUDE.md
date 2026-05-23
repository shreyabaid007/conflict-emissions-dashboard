# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Identity

**Name:** War Carbon Emissions Dashboard (WCED)
**Mission:** A near-real-time, publicly auditable dashboard quantifying CO₂ emissions from oil and fuel infrastructure fires during the 2026 Iran–US–Israel war, using only public satellite data and peer-reviewed emission factors.
**Stage:** V1 — single category (oil/fuel fire emissions only). Architecture must support future emission-category modules.

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
- **Satellite data:** sentinelsat, pystac-client, planetary-computer (Microsoft Planetary Computer)
- **Atmospheric modeling:** HARP for TROPOMI, optional HYSPLIT via Docker
- **AI/LLM:** Anthropic Claude API (claude-opus-4-7 for complex reasoning, claude-haiku-4-5 for high-volume classification); structured outputs via Pydantic models
- **Vision:** Hugging Face transformers (ViT, CLIP); fine-tuning on xView2-style damage datasets later
- **Monte Carlo:** NumPy + SciPy; consider PyMC for hierarchical models in V2
- **Storage:** PostgreSQL + PostGIS (events, facilities, estimates); MinIO/S3 (raster tiles); DuckDB for analytics
- **Pipeline orchestration:** Prefect (preferred over Airflow for Python-native DX)
- **Frontend:** Next.js + MapLibre GL (no proprietary mapping); served separately from API
- **Observability:** OpenTelemetry; Sentry for errors; structured JSON logs
- **Deployment:** Docker Compose for dev; Kubernetes-ready for prod (Helm charts in `deploy/`)

## Repository Structure

See `structure.md` for canonical layout. Key directories:
- `wced/` — main Python package
- `wced/ingest/` — data source connectors (one module per source)
- `wced/detect/` — fire detection logic
- `wced/verify/` — verification pipeline (satellite + ACLED + LLM)
- `wced/quantify/` — emissions calculations (FRP, inventory, Monte Carlo)
- `wced/ai/` — Claude/vision model wrappers with provenance tracking
- `wced/provenance/` — provenance graph data model and storage
- `wced/api/` — FastAPI app
- `wced/cli/` — Typer CLI for ops tasks
- `data/` — git-LFS facility registry; emission factor YAML; NEVER raw satellite data
- `methodology/` — versioned methodology docs (PDF + LaTeX source)
- `tests/` — pytest; aim for ≥80% coverage on `quantify/` and `provenance/`
- `notebooks/` — exploratory only; never in pipeline

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

- Methodology is versioned with semantic versioning (v1.0, v1.1, v2.0)
- Database stores `methodology_version` on every estimate
- Recomputing all estimates is a deliberate operation, never automatic
- The methodology PDF must be approved by the Scientific Steering Committee before being released as a version

## Editorial Workflow

- New incidents enter `pending_review` status
- Editorial board reviewer approves → `published`
- Reviewer rejects → `rejected` with reason
- Published incidents that fail later verification → `retracted` with public changelog entry
- Never silently delete; always changelog

## Useful Documents

- `docs/V1_PLAN.md` — V1 scope and methodology
- `.steering/product.md` — product vision and user personas
- `.steering/structure.md` — repo layout
- `.steering/tech.md` — tech stack rationale
- `methodology/v1.0.pdf` — the source of truth for all equations

## When Stuck

If methodology is unclear: stop, ask. Don't invent.
If a source seems unreliable: stop, flag for editorial review.
If an AI output disagrees with a deterministic calculation: trust the deterministic one.
If uncertainty bounds seem implausibly narrow: they probably are; revisit the parameter PDFs.

## Deferred Decisions (must resolve before the listed prompt)

- **Emission factor → facility type binding (before Prompt 4.2):**
  Each entry in `data/emission_factors.yaml` should include an
  `applicable_facility_types` list (e.g., `[REFINERY, OIL_DEPOT]` for
  `crude_oil_combustion`; `[GAS_PROCESSING]` for a future natural-gas
  factor). `quantify/inventory.py` must validate that the selected
  factor is applicable to the event's facility type and raise if not.
  Without this guard, applying a crude-oil combustion factor to a
  gas-processing facility silently produces wrong numbers.

  - **Methodology PDF v1.0 (before Prompt 4.1):** Write
  `methodology/v1.0.tex` covering Sections 2 (data model), 3 (emission
  calculations — FRP method §3.3, inventory method §3.4, reconciliation
  §3.5), and 4 (verification and confidence labels §4.3). Compile to
  `methodology/v1.0.pdf`. Content source: `docs/V1_PLAN.md` Sections
  "V1 Methodology Specification" and "What the V1 Dashboard Displays".
  No quantification code merges without this PDF existing.