# WCED Operational Runbook

## First-Time Setup

```bash
# 1. Clone and install
git clone <repo>
cd war-emission-tracker
cp .env.example .env
# Fill in: FIRMS_MAP_KEY, ACLED_EMAIL, ACLED_PASSWORD, WCED_ANTHROPIC_API_KEY

# 2. Start the dev stack
just bootstrap    # runs: docker compose up -d → wait → migrate

# 3. Load the facility registry
just facility-load
# Loads data/facilities/iran_oil_gas.geojson into the facilities table.

# 4. (Optional) Ingest pre-war baseline data
# Required for accurate baseline subtraction on facilities with routine flaring.
docker compose -f deploy/docker-compose.yml exec wced-api \
  wced ingest firms-historical --start 2025-02-28 --end 2026-02-27
```

## Daily Ingestion Cycle

Run these in order. Each is idempotent — re-running for the same date is safe.

```bash
# 1. Ingest today's FIRMS data
just ingest                     # or: just ingest 2026-03-15

# 2. Ingest today's ACLED data (for corroboration)
just ingest-acled               # or: just ingest-acled 2026-03-15

# 3. Run fire-event detection
just detect                     # --no-auto-publish is enforced
# New events land in PENDING_REVIEW.

# 4. Review and approve events
just verify                     # lists pending events
# Then approve individually:
docker compose -f deploy/docker-compose.yml exec wced-api \
  wced verify approve <event-id> --reviewer <your-name>

# 5. Quantify emissions for all published events
just quantify
```

## Weekly Recompute

When parameter distributions or damage assessments change, recompute all
estimates under the current methodology version:

```bash
docker compose -f deploy/docker-compose.yml exec wced-api \
  wced recompute --methodology-version 1.0.5
```

This creates new emission_estimates rows with updated provenance. Old rows
are never deleted.

## How to Add a Facility

1. Add the facility to `data/facilities/iran_oil_gas.geojson` following the
   existing schema (see `data/facilities/facilities.schema.json`).
   Required fields: name, facility_type, capacity_barrels, geometry.

2. Reload the facility registry:
   ```bash
   just facility-load
   ```

3. Re-run detection to pick up any historical FIRMS hotspots near the new
   facility:
   ```bash
   just detect
   ```

## How to Approve Events with Damage Assessments

For events where Sentinel-2 imagery shows the fraction destroyed:

```bash
# Attach a damage assessment to a published event
docker compose -f deploy/docker-compose.yml exec wced-api \
  wced verify add-assessment <event-id> \
    --fraction-destroyed 0.15 \
    --method EXPERT_ESTIMATE \
    --reviewer <your-name>

# Then re-quantify to pick up the new assessment
just quantify
```

## How to Bump Methodology Version

1. Document the change in `methodology/CHANGELOG.md` with: version, date,
   type (calibration/data fix/structural), description of what changed,
   expected impact, and affected outputs.

2. If the change modifies equations or parameters: create a new LaTeX file
   `methodology/v<new>.tex` (do not edit frozen versions).

3. Update `WCED_METHODOLOGY_VERSION` in `.env` (and `.env.example`).

4. Recompute all affected estimates:
   ```bash
   docker compose -f deploy/docker-compose.yml exec wced-api \
     wced recompute --methodology-version <new-version>
   ```

5. Update `CLAUDE.md` methodology version references.

## Common Errors and Diagnosis

### FIRMS API returns 403 or rate-limited
The free FIRMS tier allows 10 requests/minute. The ingest connector has
built-in exponential backoff via tenacity. If persistent, check that
`FIRMS_MAP_KEY` in `.env` is valid and not expired.

### Baseline shows `insufficient_baseline_history`
The 30-day rolling baseline window found < 5 pre-war observations. Either:
- The facility is new and has no pre-war FIRMS data → run `firms-historical`
  backfill for the relevant date range.
- The facility is in a region with sparse VIIRS/MODIS coverage → the fallback
  baseline (0 MW mean, 50 MW std) is applied automatically.

### All storage-type events show `needs_review=True`
The FRP/inventory ratio is outside [0.5, 2.0]. Common causes:
- Fraction-destroyed default is too high → recalibrate in
  `parameter_distributions.yaml` and bump methodology version.
- Facility capacity is wrong → update `iran_oil_gas.geojson`.

### Docker containers won't start
```bash
just logs                       # check all service logs
just logs wced-api              # check specific service
```
Port conflicts: override in `.env` (`POSTGRES_PORT`, `API_PORT`, etc.).
Nuclear option: `just nuke && just bootstrap` (destroys all data volumes).

### Database migration fails
```bash
just psql                       # open a psql shell to inspect state
# Check current migration:
SELECT version_num FROM alembic_version;
```

## Service Ports (defaults)

| Service | Port |
|---------|------|
| PostgreSQL | 5432 |
| Redis | 6379 |
| MinIO API | 9000 |
| MinIO Console | 9001 |
| Prefect UI | 4200 |
| WCED API | 8000 |
| Frontend | 3000 |
