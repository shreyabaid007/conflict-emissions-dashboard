# tech.md — Tech Stack Rationale

## Language: Python 3.11+

Geospatial + ML + scientific computing ecosystem is Python-native. 3.11 for performance + better error messages. Use `match` statements for verification state machines.

## Package Management: uv

`uv` is fast, reproducible, and works with PEP 621 `pyproject.toml`. Lockfile committed.

## Data Validation: Pydantic v2

- All data crossing module boundaries is Pydantic models
- v2 for performance (Rust core) and `model_validator` for cross-field invariants
- Validators enforce methodology invariants (e.g., "p5 <= p50 <= p95")

## API: FastAPI

- Native Pydantic integration
- Automatic OpenAPI for our public API consumers
- Async-first for satellite-data ingestion fan-out

## Geospatial Stack

- **GeoPandas + Shapely** for vector operations (facility polygons, spatial joins)
- **Rasterio** for satellite rasters
- **pystac-client + planetary-computer** for Sentinel access (free, no API key juggling)
- **xarray + rioxarray** for multi-dimensional satellite data
- **HARP** specifically for TROPOMI L2 — handles the netCDF + S5P quirks

## Atmospheric / Dispersion Modeling

- **HYSPLIT** via NOAA's container; called from Python wrapper
- **FLEXPART** as alternative for sensitivity comparison
- Both wrapped behind a `DispersionModel` protocol so they're interchangeable

## AI / LLM

- **Anthropic Claude API** — claude-opus-4-7 for complex reasoning (OSINT triage on long news articles, multi-source consistency analysis), claude-haiku-4-5 for high-volume classification
- **Structured outputs** via Pydantic models so AI outputs are typed
- **Token logging** per AI call → provenance record
- **Prompt versioning** — prompts live in `wced/ai/prompts/` as templates, versioned in git
- Always set `temperature=0` for deterministic classification tasks; allow higher temperature only for triage/exploration with multiple samples

## Vision Models

- **Hugging Face transformers** for fine-tuned ViT/CLIP on Sentinel-2 RGB+SWIR composites
- **Local inference** preferred for high-volume classification (cost + provenance)
- **xView2 / SpaceNet** datasets for damage-classification fine-tuning in V2

## Monte Carlo

- **NumPy + SciPy.stats** for V1 — sufficient for the parametric distributions we use
- **PyMC** considered for V2 hierarchical models (e.g., facility-type-conditional priors)
- Always seed RNG explicitly for reproducibility; seed stored in estimate record

## Storage

### PostgreSQL + PostGIS
- Authoritative store for facilities, events, estimates, provenance
- PostGIS for spatial queries (e.g., "fires within 500m of refineries")
- Logical replication for read replicas

### Object storage (MinIO / S3-compatible)
- Raster tiles, cropped Sentinel chips, exported CSVs
- Content-addressed where possible

### DuckDB
- Analytics queries from CSV/Parquet exports
- Frontend reads aggregated parquet, never live OLTP

### Redis
- Rate limit tracking for external API calls (FIRMS, ACLED)
- Cache for facility geometries
- Job queue if Prefect is overkill (V1 starts with Prefect)

## Pipeline Orchestration: Prefect

- Python-native (no XML, no decorators that feel alien)
- Prefect 3.x has good async support
- Local Prefect server in dev; Prefect Cloud or self-hosted in prod
- Each Flow is a single responsibility (`daily_firms_ingest`, `weekly_validation`)
- Flows are idempotent — re-running for a date does not duplicate

## Observability

- **structlog** for JSON logs with bound context (event_id, facility_id flow through)
- **OpenTelemetry** for tracing across the pipeline (Prefect → API)
- **Sentry** for exception aggregation
- **Prometheus + Grafana** for ops metrics (ingest lag, MC sample count, AI token spend)

## Frontend: Next.js + MapLibre GL

- Next.js App Router with React Server Components for the static-heavy methodology / changelog pages
- MapLibre GL (not Mapbox) — open source, no token, no vendor lock
- Self-hosted vector tiles via OpenMapTiles
- Charts: Visx or D3; never proprietary chart libraries
- No analytics tracking by default — respect user privacy

## Testing

- **pytest** + pytest-cov + pytest-asyncio
- **Hypothesis** for property-based tests on `quantify/` invariants
- **Schemathesis** for API contract testing
- **VCR.py** for cassette-based ingest tests (no live API calls in CI)

## CI/CD

- **GitHub Actions**
- Required checks: ruff (lint + format), mypy strict, pytest, methodology-compliance suite
- Methodology compliance: a special test suite that ensures published numbers from the methodology PDF can be reproduced by running the code on snapshot inputs

## Security

- Secrets via `pydantic-settings` from environment / 1Password Connect
- No secrets in git; pre-commit hook scans for them
- API tokens for external services rotated quarterly
- Read-only DB user for frontend reads
- All external API calls go through outbound proxy logged in audit trail

## Documentation

- **MkDocs Material** for the public-facing docs site
- Methodology PDF generated from LaTeX, never edited in Word
- Auto-generated API docs from FastAPI OpenAPI

## Why NOT These Things

- **Not Airflow** — too XML-y, too JVM-y, slow Python integration
- **Not Pandas-first** — GeoPandas where geo, Polars/DuckDB for analytics; Pandas only at thin edges
- **Not LangChain** — too much indirection, abstracts away the prompt provenance we need
- **Not vendor lock-in mapping** (Mapbox, Google) — undermines open-source positioning
- **Not Tableau / PowerBI** — closed, hard to version
- **Not auto-deploying ML models to production without methodology PDF update** — model is part of methodology
- **Not "real-time" anything that takes >1 hour to update** — honesty in tech choices

## Performance Targets

- API p95 latency < 300ms for all read endpoints
- Daily FIRMS ingest completes within 30 minutes
- Monte Carlo (10,000 iterations) for a single event < 5 seconds
- Full reanalysis (recompute all estimates for new methodology version) < 4 hours

## Cost Targets (V1, monthly)

- Infrastructure: < $500/mo (small VPS + S3 + PG instance)
- Claude API: < $1,000/mo (budget per AI module separately; alert on anomaly)
- Planet Labs tasked imagery: budget separately, only for high-priority events
- All other satellite data: free (Sentinel via Planetary Computer, FIRMS, MODIS)
