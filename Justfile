set dotenv-load

compose := "docker compose -f deploy/docker-compose.yml"

# Start all infrastructure services
up:
    {{compose}} up -d

# Start everything including the frontend (dev profile)
up-dev:
    {{compose}} --profile dev up -d

# Stop all services
down:
    {{compose}} --profile dev down

# Stop and destroy volumes
nuke:
    {{compose}} --profile dev down -v

# Show service logs (follow)
logs *args="":
    {{compose}} logs -f {{args}}

# Run database migrations
migrate:
    {{compose}} exec wced-api wced db migrate --yes

# Load the seeded facility registry into the facilities table
facility-load:
    {{compose}} exec wced-api wced facility load --path /app/data/facilities/iran_oil_gas.geojson --yes

# Ingest FIRMS data for a given date (default: today)
ingest date="today":
    #!/usr/bin/env bash
    if [ "{{date}}" = "today" ]; then
        d=$(date -u +%Y-%m-%d)
    else
        d="{{date}}"
    fi
    {{compose}} exec wced-api wced ingest firms --date "$d" --yes

# Ingest ACLED data for a given date (default: today)
ingest-acled date="today":
    #!/usr/bin/env bash
    if [ "{{date}}" = "today" ]; then
        d=$(date -u +%Y-%m-%d)
    else
        d="{{date}}"
    fi
    {{compose}} exec wced-api wced ingest acled --date "$d" --yes

# Run fire-event detection (new events land in PENDING_REVIEW)
detect since="2026-02-28":
    {{compose}} exec wced-api wced detect --since {{since}} --no-auto-publish --yes

# Quantify emissions for all published events
quantify:
    {{compose}} exec wced-api wced quantify --all-published --yes

# Run the editorial verification CLI
verify:
    {{compose}} exec wced-api wced verify pending

# Export a static API snapshot into frontend/public/api-snapshot/ (for Vercel)
export-snapshot api_url="http://localhost:8000":
    python scripts/export_snapshot.py --api-url {{api_url}}

# Run tests
test *args="":
    python -m pytest tests/ -v {{args}}

# Run linter
lint:
    ruff check wced/ tests/
    ruff format --check wced/ tests/

# Auto-fix lint issues
fix:
    ruff check --fix wced/ tests/
    ruff format wced/ tests/

# Type-check strict modules
typecheck:
    mypy wced/quantify wced/provenance

# Rebuild containers
build:
    {{compose}} --profile dev build

# Open psql shell
psql:
    {{compose}} exec postgres psql -U wced

# Open MinIO console (prints URL)
minio-console:
    @echo "http://localhost:${MINIO_CONSOLE_PORT:-9001}"

# Open Prefect UI (prints URL)
prefect-ui:
    @echo "http://localhost:${PREFECT_PORT:-4200}"

# Full bootstrap: up, migrate, seed
bootstrap:
    just up
    @echo "Waiting for services..."
    sleep 5
    just migrate
    @echo "Ready. API at http://localhost:${API_PORT:-8000}, Prefect at http://localhost:${PREFECT_PORT:-4200}"
