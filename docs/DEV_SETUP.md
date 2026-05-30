# Local Development Setup

## Prerequisites

- Docker and Docker Compose v2
- Python 3.11+ (for running tests and linting outside Docker)
- [just](https://github.com/casey/just) command runner (`brew install just` / `cargo install just`)
- Node.js 20+ (only if running frontend outside Docker)

## Quick Start

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env — fill in FIRMS_MAP_KEY, ACLED_EMAIL, ACLED_PASSWORD, ANTHROPIC_API_KEY

# 2. Start infrastructure + API + worker
just up
# Or with frontend:
just up-dev

# 3. Run database migrations
just migrate

# 4. Ingest today's FIRMS data
just ingest
# Or for a specific date:
just ingest 2026-03-15

# 5. Open the dashboard
# API:      http://localhost:8000
# Frontend: http://localhost:3000  (requires `just up-dev`)
# Prefect:  http://localhost:4200
# MinIO:    http://localhost:9001
```

## Services

| Service | Port | Purpose |
|---------|------|---------|
| `postgres` | 5432 | PostgreSQL 16 + PostGIS 3.4 |
| `redis` | 6379 | Caching and task queues |
| `minio` | 9000 (API), 9001 (console) | S3-compatible raster storage |
| `prefect-server` | 4200 | Pipeline orchestration UI |
| `wced-api` | 8000 | FastAPI application |
| `wced-worker` | — | Prefect worker for pipeline flows |
| `frontend` | 3000 | Next.js dashboard (dev profile only) |

## Common Commands

```bash
just up              # Start infra + app services
just up-dev          # Start everything including frontend
just down            # Stop all services
just logs            # Tail all service logs
just logs wced-api   # Tail a specific service

just migrate         # Apply database migrations
just ingest          # Ingest FIRMS data for today
just ingest-acled    # Ingest ACLED data for today
just detect          # Run fire-event detection
just quantify        # Quantify all published events
just verify          # List events pending editorial review

just test            # Run pytest
just lint            # Check code style (ruff)
just fix             # Auto-fix lint issues
just typecheck       # Run mypy on strict modules

just psql            # Open a psql shell
just build           # Rebuild Docker images
just nuke            # Stop services and destroy all data volumes
just bootstrap       # Full setup: up + wait + migrate
```

## Hot Reload

Source directories are bind-mounted into containers for live reload during development:

- **wced-api / wced-worker**: `wced/` and `data/` are mounted read-only. Uvicorn picks up Python changes automatically. If you change `pyproject.toml` (new dependency), rebuild with `just build`.
- **frontend**: The entire `frontend/` directory is mounted. Next.js dev server hot-reloads on file changes. `node_modules/` and `.next/` are kept in anonymous volumes to avoid overwriting container installs.

## Environment Variables

All variables are documented in `.env.example`. The compose file passes them through to containers automatically. Key variables:

| Variable | Required | Purpose |
|----------|----------|---------|
| `FIRMS_MAP_KEY` | Yes | NASA FIRMS API key |
| `ACLED_EMAIL` | Yes | ACLED account email (OAuth username) |
| `ACLED_PASSWORD` | Yes | ACLED account password (OAuth password grant) |
| `ANTHROPIC_API_KEY` | For AI features | Claude API for severity extraction |
| `POSTGRES_PASSWORD` | No | Defaults to `wced` |
| `WCED_METHODOLOGY_VERSION` | No | Defaults to `1.0` |

## Running Tests

Tests run on the host (not inside Docker) against a local Python environment:

```bash
pip install -e ".[dev]"
just test

# With coverage
just test --cov-report=html

# Specific test file
just test tests/unit/quantify/test_frp.py
```

Integration tests that need PostGIS are marked with `@pytest.mark.integration` and expect the Docker postgres to be running.

## Troubleshooting

**Port conflicts**: Override default ports in `.env`:
```
POSTGRES_PORT=5433
API_PORT=8001
FRONTEND_PORT=3001
```

**Database issues**: Reset everything with `just nuke && just bootstrap`.

**Container won't start**: Check logs with `just logs <service-name>`.
