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

- **HYSPLIT** via NOAA's container; called from Python wrapper (planned, not yet implemented)
- Wrapped behind a `DispersionModel` protocol so alternatives can plug in

## AI / LLM

- **Anthropic Claude API** — claude-opus-4-7 for complex reasoning (OSINT triage on long news articles, multi-source consistency analysis), claude-haiku-4-5 for high-volume classification
- **OpenRouter** supported as alternative AI provider (`WCED_AI_PROVIDER=openrouter`); auto-detected if only OpenRouter key is set
- **Structured outputs** via Pydantic models so AI outputs are typed
- **Token logging** per AI call → provenance record
- Always set `temperature=0` for deterministic classification tasks; allow higher temperature only for triage/exploration with multiple samples

## Vision Models

- **Hugging Face transformers** for fine-tuned ViT/CLIP on Sentinel-2 RGB+SWIR composites (V2 planned)
- **xView2 / SpaceNet** datasets for damage-classification fine-tuning in V2

## Monte Carlo

- **NumPy + SciPy.stats** for V1 — sufficient for the parametric distributions we use
- **PyMC** considered for V2 hierarchical models (e.g., facility-type-conditional priors)
- Always seed RNG explicitly for reproducibility; seed stored in estimate record

## Storage

### PostgreSQL + PostGIS
- Authoritative store for facilities, events, estimates, provenance
- PostGIS for spatial queries (e.g., "fires within 500m of refineries") via **GeoAlchemy2**
- Logical replication for read replicas

### Object storage (MinIO / S3-compatible)
- Raster tiles, cropped Sentinel chips, exported CSVs
- Content-addressed where possible

### DuckDB
- Analytics queries from CSV/Parquet exports
- Frontend reads aggregated parquet, never live OLTP

## Task Runner: Justfile

- `just` recipes for common dev operations (`just up`, `just detect`, `just quantify`)
- Wraps `docker compose exec` commands for consistent container execution
- Replaces ad-hoc shell scripts for pipeline operations

## Pipeline Orchestration

- Python modules in `wced/pipeline/` (`daily_ingest`, `quantification`, `validation_weekly`)
- CLI commands in `wced/cli/main.py` serve as the primary orchestration interface
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
- **VCR.py** for cassette-based ingest tests (no live API calls in CI)
- **schemathesis** for API contract testing against OpenAPI schema

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

- Methodology PDF generated from LaTeX, never edited in Word
- Auto-generated API docs from FastAPI OpenAPI
- Steering docs in `.steering/` for Claude Code context

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

## Deployment (v2)

- **Compute & API:** Modal.com (serverless). Daily ingest via `modal.Cron`; FastAPI served via `@modal.asgi_app` with scale-to-zero. Funded by $1,000 Modal credits — no recurring cash cost until credits exhaust.
- **Database:** Neon (recommended) or Supabase — free tier with PostGIS. Neon scales to zero (matches Modal); Supabase free tier pauses after 7 days idle but the daily Modal cron keeps it awake.
- **Frontend:** Vercel or Cloudflare Pages — free tier (Next.js deploys free).
- **Agent orchestration:** Local machine. Claude Code / Hermes / Paperclip run on the Max 5x allocation. Single-agent passes only — avoid parallel agent teams that blow weekly rate limits.
- **Docker Compose remains for local dev.** The `just up` workflow is unchanged.

## Cost Targets (v2, monthly)

- **$0/mo additional cash.** Claude Max 5x ($100/mo) is already owned and covers all agent work. Modal credits cover compute. Postgres, frontend, and all satellite data sources are free-tier.
- First real cash cost only appears when (a) Modal credits exhaust, or (b) data outgrows the free Postgres tier — at which point a ~$50/mo VPS or ~$19/mo Neon Launch plan is the next step.
- Planet Labs tasked imagery: budget separately, only for high-priority events.
- All satellite data: free (Sentinel via Planetary Computer, FIRMS, MODIS).
