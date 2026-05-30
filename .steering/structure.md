# structure.md — Repository Layout

```
war-emission-tracker/
├── CLAUDE.md                       # Claude Code steering
├── HANDOFF.md                      # Current state snapshot (numbers, pending work)
├── README.md                       # Public-facing intro
├── pyproject.toml                  # uv / hatch / poetry
├── .python-version                 # 3.11+
├── .pre-commit-config.yaml         # ruff + mypy + nbstripout
├── .env.example                    # never commit .env
├── alembic.ini                     # Alembic migration config
├── Justfile                        # task runner (just detect, just up, etc.)
│
├── .steering/                      # AI assistant steering files
│   ├── product.md                  # product vision
│   ├── structure.md                # this file
│   └── tech.md                     # tech stack rationale
│
├── docs/
│   ├── V1_PLAN.md                  # V1 scope and methodology summary (with current status)
│   ├── DEV_SETUP.md                # developer setup guide
│   ├── RUNBOOK.md                  # operational runbook for running the pipeline
│   ├── PROMPTS.md                  # historical record of V1 build prompts
│   ├── INCIDENT_RESPONSE.md        # how to handle errors and retractions
│   └── LAUNCH_CHECKLIST.md         # pre-launch verification steps
│
├── methodology/
│   ├── v1.0.pdf                    # base methodology (frozen)
│   ├── v1.0.tex                    # LaTeX source for v1.0
│   ├── v1.0.5.tex                  # current methodology (supersedes v1.0)
│   └── CHANGELOG.md                # what changed between versions (v1.0 → v1.0.5)
│
├── data/
│   ├── facilities/
│   │   ├── iran_oil_gas.geojson    # curated facility geometries
│   │   └── facilities.schema.json  # JSON schema for facility registry
│   ├── emission_factors.yaml       # all factors with citations
│   └── parameter_distributions.yaml # PDFs for Monte Carlo (k_ext, duty cycle)
│
├── wced/                           # main Python package
│   ├── __init__.py
│   ├── settings.py                 # Pydantic settings (env vars, DB URL, API keys)
│   ├── logging.py                  # structlog setup
│   │
│   ├── models/                     # Pydantic data models
│   │   ├── __init__.py
│   │   ├── facility.py             # Facility, FacilityType
│   │   ├── event.py                # FireEvent, DetectionSource, ConfidenceLabel
│   │   ├── provenance.py           # ProvenanceRecord, Source, SourceType
│   │   ├── editorial.py            # EditorialStatus, EditorialAction, state machine
│   │   └── assessment.py           # DamageAssessment (fraction destroyed distribution)
│   │
│   ├── ingest/                     # one module per source
│   │   ├── __init__.py
│   │   ├── base.py                 # IngestConnector protocol, BBox type
│   │   ├── firms.py                # NASA FIRMS API (NRT + SP archival)
│   │   ├── sentinel2.py            # Sentinel-2 via Planetary Computer
│   │   ├── sentinel5p.py           # TROPOMI NO₂/SO₂
│   │   ├── gdelt.py                # GDELT conflict events (primary)
│   │   ├── ucdp.py                 # UCDP georeferenced events (historical validation)
│   │   └── acled.py                # ACLED conflict events (behind feature flag; requires paid access)
│   │
│   ├── categories/                 # emission category protocol + plugins
│   │   ├── __init__.py
│   │   ├── base.py                 # EmissionCategory Protocol + CategoryRegistry
│   │   └── oil_fuel_fire/          # oil/fuel fire category (first implementation)
│   │       ├── __init__.py
│   │       └── category.py         # OilFuelFireCategory wrapping detect/verify/quantify
│   │
│   ├── detect/                     # fire detection logic
│   │   ├── __init__.py
│   │   ├── hotspot.py              # FIRMS → candidate fire events
│   │   ├── facility_match.py       # spatial join with facility registry
│   │   ├── baseline.py             # rolling p75 FRP baseline subtraction
│   │   └── persistence.py          # multi-overpass persistence filter
│   │
│   ├── verify/                     # verification pipeline
│   │   ├── __init__.py
│   │   ├── sentinel2_check.py      # optical confirmation
│   │   ├── acled_corroboration.py  # ACLED corroboration (behind feature flag)
│   │   ├── corroboration.py        # multi-source corroboration logic
│   │   ├── confidence.py           # confidence label assignment
│   │   └── editorial.py            # editorial review queue (Postgres + in-memory)
│   │
│   ├── quantify/                   # emissions calculations (pure functions)
│   │   ├── __init__.py
│   │   ├── distribution.py         # Distribution class — the core type
│   │   ├── frp.py                  # FRP-based emissions (methodology §3.3)
│   │   ├── inventory.py            # inventory-based emissions (methodology §3.4)
│   │   ├── factors.py              # emission factor + parameter distribution lookup
│   │   ├── aggregate.py            # aggregate estimates across events/facilities
│   │   └── reconcile.py            # FRP vs inventory cross-check (§3.5)
│   │
│   ├── validate/                   # top-down validation
│   │   ├── __init__.py
│   │   └── tropomi.py              # plume detection in TROPOMI
│   │
│   ├── ai/                         # LLM and vision integrations
│   │   ├── __init__.py
│   │   ├── claude_client.py        # Anthropic client wrapper with provenance
│   │   └── classify.py             # vision classification (fire/flaring/false)
│   │
│   ├── provenance/                 # provenance graph
│   │   ├── __init__.py
│   │   └── store.py                # InMemoryProvenanceStore + persistence
│   │
│   ├── pipeline/                   # orchestration flows
│   │   ├── __init__.py
│   │   ├── daily_ingest.py         # daily FIRMS + GDELT ingest
│   │   ├── facility_repo.py        # facility registry management
│   │   ├── quantification.py       # batch quantification flow
│   │   └── validation_weekly.py    # weekly TROPOMI validation
│   │
│   ├── api/                        # FastAPI app
│   │   ├── __init__.py
│   │   ├── main.py                 # app factory
│   │   ├── dependencies.py         # DI for DB sessions, settings
│   │   ├── middleware/
│   │   │   └── telemetry.py        # OpenTelemetry middleware
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── events.py           # /events endpoints
│   │   │   ├── facilities.py       # /facilities endpoints
│   │   │   ├── aggregates.py       # /aggregates endpoints
│   │   │   ├── timeseries.py       # /timeseries endpoints
│   │   │   └── meta.py             # /meta (health, methodology version)
│   │   └── schemas/
│   │       ├── __init__.py
│   │       └── responses.py        # API response schemas
│   │
│   ├── db/                         # database layer
│   │   ├── __init__.py
│   │   ├── session.py              # SQLAlchemy async session
│   │   ├── models.py               # SQLAlchemy ORM (all tables)
│   │   ├── migrations/
│   │   │   ├── env.py              # Alembic environment
│   │   │   └── versions/
│   │   │       └── 001_initial_schema.py
│   │   └── repositories/           # one repo per aggregate
│   │       ├── __init__.py
│   │       ├── facility.py
│   │       ├── fire_event.py
│   │       ├── emission.py
│   │       ├── damage.py           # DamageAssessmentRepository
│   │       ├── editorial.py
│   │       ├── provenance.py
│   │       ├── ingestion.py
│   │       ├── pipeline.py
│   │       └── validation.py
│   │
│   └── cli/                        # Typer CLI for ops
│       ├── __init__.py
│       ├── main.py                 # all commands: detect, quantify, recompute, ingest, backfill, etc.
│       └── verify.py               # verify approve/reject/resubmit/retract/add-assessment
│
├── frontend/                       # Next.js app, separate deployment
│   ├── package.json
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   ├── app/
│   │   ├── layout.tsx              # root layout with providers
│   │   ├── page.tsx                # headline dashboard
│   │   ├── providers.tsx           # React context providers
│   │   ├── globals.css
│   │   ├── map/page.tsx            # map view
│   │   ├── event/[id]/page.tsx     # event detail with provenance chain
│   │   ├── methodology/page.tsx
│   │   └── changelog/page.tsx
│   ├── components/
│   │   ├── Header.tsx
│   │   ├── Footer.tsx
│   │   ├── HeadlineCard.tsx        # p5/p50/p95 summary card
│   │   ├── UncertaintyBar.tsx      # visual uncertainty range
│   │   ├── EventMap.tsx            # MapLibre GL event map
│   │   ├── ProvenanceChain.tsx     # source → estimate provenance display
│   │   ├── EmissionTimeline.tsx    # time-series emission chart
│   │   ├── CumulativeChart.tsx     # cumulative emissions chart
│   │   ├── RecentEventsList.tsx    # recent events sidebar
│   │   ├── DisclaimerModal.tsx     # methodology disclaimer
│   │   ├── MethodologyBanner.tsx   # methodology version banner
│   │   └── Tooltip.tsx
│   └── lib/
│       ├── api.ts                  # typed API client
│       ├── constants.ts            # shared constants
│       └── format.ts               # number/date formatting utilities
│
├── tests/
│   ├── unit/
│   │   ├── quantify/               # high coverage required
│   │   ├── provenance/
│   │   ├── ai/
│   │   ├── api/                    # route + contract tests
│   │   ├── db/repositories/
│   │   ├── detect/
│   │   ├── ingest/
│   │   ├── models/
│   │   ├── pipeline/
│   │   ├── scripts/
│   │   ├── validate/
│   │   └── verify/
│   ├── integration/
│   │   ├── db/                     # repository integration tests
│   │   ├── ingest/
│   │   └── pipeline/
│   ├── fixtures/
│   │   ├── snapshots/              # known-good test snapshots
│   │   └── cassettes/              # VCR cassettes for GDELT, FIRMS, etc.
│   └── methodology/                # tests that implementation matches the PDF
│       ├── test_eq_3_3_frp_emissions.py
│       ├── test_eq_3_4_inventory_emissions.py
│       ├── test_eq_3_5_reconciliation.py
│       ├── test_factors_match_pdf_table_2.py
│       └── test_priors_match_pdf_table_3.py
│
├── scripts/                        # one-off operational scripts
│   ├── bootstrap_facilities.py     # seed facility registry from GeoJSON
│   ├── backfill_full_range.py      # bulk FIRMS backfill
│   ├── approve_top_events.sh       # editorial approval batch script
│   ├── extract_pdf_examples.py     # extract test values from methodology PDF
│   ├── launch_check.py             # pre-launch verification
│   └── update_storage_assessments_v105.sh  # v1.0.5 damage assessment migration
│
├── deploy/
│   ├── docker-compose.yml          # dev stack (api, db, frontend, grafana, prometheus)
│   ├── Dockerfile.api              # wced API + CLI
│   ├── Dockerfile.frontend         # Next.js frontend
│   ├── Dockerfile.pipeline         # pipeline worker
│   ├── postgres-init/              # DB init scripts
│   ├── grafana/                    # Grafana dashboards + datasources
│   ├── prometheus.yml              # Prometheus scrape config
│   └── helm/                       # production K8s charts (placeholder)
│
└── .github/
    ├── workflows/
    │   └── ci.yml                  # ruff + mypy + pytest
    └── PULL_REQUEST_TEMPLATE.md
```

## Layout Rules

1. **Models are Pydantic-only.** Database ORM lives in `db/models.py`, kept separate to allow data model evolution independent of storage schema.
2. **`quantify/` is pure.** No I/O. Takes data, returns Distribution. Easy to test.
3. **`ai/` is bounded.** Every AI call goes through `claude_client.py` so prompts, tokens, and provenance are uniformly logged.
4. **`provenance/` is sacrosanct.** Anything that produces a number depends on it.
5. **`ingest/` modules are interchangeable.** Each implements `IngestConnector` protocol so new sources plug in without changing pipelines.
6. **API schemas are separate from internal models.** API contracts shouldn't leak internal data shape.
7. **Methodology PDF is in `methodology/`, not in code.** Code references PDF section numbers in docstrings.
8. **CLI is monolithic per concern.** `cli/main.py` holds all pipeline commands; `cli/verify.py` holds editorial commands. No separate file per subcommand.

## Naming Conventions

- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Pydantic models suffix with type: `FireEvent`, `EmissionEstimate`
- Pure functions in `quantify/` start with verb: `compute_*`, `sample_*`, `reconcile_*`
- Test files: `test_<module>.py`
- Test methodology compliance: `tests/methodology/test_eq_<section>.py`
