# structure.md — Repository Layout

```
war-carbon-dashboard/
├── CLAUDE.md                       # Claude Code steering
├── README.md                       # Public-facing intro
├── LICENSE                         # MIT for code
├── DATA_LICENSE                    # CC-BY 4.0 for data outputs
├── pyproject.toml                  # uv / hatch / poetry
├── .python-version                 # 3.11+
├── .pre-commit-config.yaml         # ruff + mypy + nbstripout
├── .env.example                    # never commit .env
│
├── .steering/                      # AI assistant steering files
│   ├── product.md                  # product vision
│   ├── structure.md                # this file
│   └── tech.md                     # tech stack rationale
│
├── docs/
│   ├── V1_PLAN.md                  # V1 scope and methodology summary
│   ├── PROMPTS.md                  # Claude Code prompts to build the project
│   ├── ARCHITECTURE.md             # diagrams and design notes
│   ├── EDITORIAL_WORKFLOW.md       # how incidents become published
│   ├── INCIDENT_RESPONSE.md        # how to handle errors and retractions
│   └── DUAL_USE_REVIEW.md          # checklist for sensitive data
│
├── methodology/
│   ├── v1.0.pdf                    # the source of truth — equations, factors, citations
│   ├── v1.0.tex                    # LaTeX source
│   ├── CHANGELOG.md                # what changed between versions
│   └── references.bib              # all citations
│
├── data/
│   ├── facilities/
│   │   ├── iran_oil_gas.geojson    # curated facility geometries
│   │   ├── gulf_oil_gas.geojson
│   │   └── facilities.schema.json
│   ├── emission_factors.yaml       # all factors with citations
│   ├── parameter_distributions.yaml # PDFs for Monte Carlo
│   └── bounding_boxes.geojson      # AOIs (Iran, Gulf, Israel)
│
├── wced/                           # main Python package
│   ├── __init__.py
│   ├── config.py                   # Pydantic settings
│   ├── logging.py                  # structlog setup
│   ├── exceptions.py               # custom exceptions
│   │
│   ├── models/                     # Pydantic data models
│   │   ├── __init__.py
│   │   ├── facility.py             # Facility, FacilityType
│   │   ├── event.py                # FireEvent, Confidence, Status
│   │   ├── estimate.py             # EmissionEstimate, Distribution
│   │   ├── provenance.py           # ProvenanceRecord, Source, ProvenanceChain
│   │   └── methodology.py          # MethodologyVersion
│   │
│   ├── ingest/                     # one module per source
│   │   ├── __init__.py
│   │   ├── base.py                 # IngestConnector protocol
│   │   ├── firms.py                # NASA FIRMS API
│   │   ├── sentinel2.py            # Sentinel-2 via Planetary Computer
│   │   ├── sentinel5p.py           # TROPOMI
│   │   ├── acled.py                # ACLED conflict events
│   │   └── news.py                 # curated news/OSINT ingestion
│   │
│   ├── detect/                     # fire detection logic
│   │   ├── __init__.py
│   │   ├── hotspot.py              # FIRMS → candidate fire events
│   │   ├── facility_match.py       # spatial join with facility registry
│   │   ├── baseline.py             # flaring baseline subtraction
│   │   └── persistence.py          # multi-overpass persistence filter
│   │
│   ├── verify/                     # verification pipeline
│   │   ├── __init__.py
│   │   ├── sentinel2_check.py      # optical confirmation
│   │   ├── acled_corroboration.py  # conflict event match
│   │   ├── confidence.py           # confidence label assignment
│   │   └── editorial.py            # editorial review queue
│   │
│   ├── quantify/                   # emissions calculations
│   │   ├── __init__.py
│   │   ├── distribution.py         # Distribution class — the core type
│   │   ├── frp.py                  # FRP-based emissions
│   │   ├── inventory.py            # inventory-based emissions
│   │   ├── factors.py              # emission factor lookup
│   │   ├── monte_carlo.py          # MC sampling orchestrator
│   │   └── reconcile.py            # FRP vs inventory cross-check
│   │
│   ├── validate/                   # top-down validation
│   │   ├── __init__.py
│   │   ├── tropomi.py              # plume detection in TROPOMI
│   │   ├── dispersion.py           # HYSPLIT/FLEXPART wrapper
│   │   └── reconcile.py            # top-down vs bottom-up comparison
│   │
│   ├── ai/                         # LLM and vision integrations
│   │   ├── __init__.py
│   │   ├── claude_client.py        # Anthropic client wrapper
│   │   ├── triage.py               # OSINT triage
│   │   ├── classify.py             # vision classification (fire/flaring/false)
│   │   ├── provenance_scorer.py    # cross-source confidence scoring
│   │   ├── factor_retrieval.py     # RAG for emission factors
│   │   └── audit.py                # methodology-vs-publication audit
│   │
│   ├── provenance/                 # provenance graph
│   │   ├── __init__.py
│   │   ├── store.py                # provenance persistence
│   │   ├── graph.py                # provenance DAG
│   │   └── render.py               # provenance chain → human-readable
│   │
│   ├── pipeline/                   # Prefect flows
│   │   ├── __init__.py
│   │   ├── daily_ingest.py
│   │   ├── verification.py
│   │   ├── quantification.py
│   │   └── validation_weekly.py
│   │
│   ├── api/                        # FastAPI app
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── routes/
│   │   │   ├── events.py
│   │   │   ├── facilities.py
│   │   │   ├── estimates.py
│   │   │   ├── timeseries.py
│   │   │   ├── methodology.py
│   │   │   └── changelog.py
│   │   └── schemas/                # API response schemas (separate from models/)
│   │
│   ├── db/                         # database layer
│   │   ├── __init__.py
│   │   ├── session.py
│   │   ├── models.py               # SQLAlchemy ORM
│   │   ├── migrations/             # Alembic
│   │   └── repositories/           # one repo per aggregate
│   │
│   └── cli/                        # Typer CLI for ops
│       ├── __init__.py
│       ├── main.py
│       ├── ingest.py
│       ├── verify.py
│       ├── recompute.py            # recompute estimates for a methodology version
│       └── audit.py
│
├── frontend/                       # Next.js app, separate deployment
│   ├── package.json
│   ├── app/
│   │   ├── page.tsx                # headline dashboard
│   │   ├── map/page.tsx            # map view
│   │   ├── event/[id]/page.tsx     # event detail
│   │   ├── methodology/page.tsx
│   │   └── changelog/page.tsx
│   ├── components/
│   │   ├── HeadlineCard.tsx
│   │   ├── UncertaintyBar.tsx
│   │   ├── EventMap.tsx
│   │   ├── ProvenanceChain.tsx
│   │   └── EmissionTimeline.tsx
│   └── lib/
│       └── api.ts                  # typed API client
│
├── tests/
│   ├── unit/
│   │   ├── quantify/               # high coverage required
│   │   ├── provenance/
│   │   └── ai/
│   ├── integration/
│   │   ├── ingest/
│   │   └── pipeline/
│   ├── fixtures/
│   │   └── snapshots/              # known-good FIRMS responses, etc.
│   └── methodology/                # tests that the implementation matches the PDF
│
├── notebooks/                      # exploratory only
│   ├── 01_explore_firms.ipynb
│   ├── 02_facility_match_spike.ipynb
│   └── 03_frp_calibration.ipynb
│
├── scripts/                        # one-off operational scripts
│   ├── bootstrap_facilities.py
│   ├── backfill_firms.py
│   └── export_for_publication.py
│
├── deploy/
│   ├── docker-compose.yml          # dev stack
│   ├── Dockerfile.api
│   ├── Dockerfile.pipeline
│   └── helm/                       # production K8s charts
│
└── .github/
    ├── workflows/
    │   ├── ci.yml                  # ruff + mypy + pytest
    │   ├── methodology_check.yml   # ensures code matches methodology PDF
    │   └── release.yml
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

## Naming Conventions

- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Pydantic models suffix with type: `FireEvent`, `EmissionEstimate`
- Pure functions in `quantify/` start with verb: `compute_*`, `sample_*`, `reconcile_*`
- Test files: `test_<module>.py`
- Test methodology compliance: `tests/methodology/test_eq_<section>.py`
