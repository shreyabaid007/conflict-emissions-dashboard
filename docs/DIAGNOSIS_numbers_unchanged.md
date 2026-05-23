# Diagnosis — Dashboard still shows v1.0.5 / unchanged numbers

**Date:** 2026-05-30
**Type:** Diagnostic only. Nothing was mutated, recomputed, published, or retracted while producing this report. All database access was read-only (`SET TRANSACTION READ ONLY` / read-only API endpoints).

---

## TL;DR (plain language)

There are **three independent problems**. **v1.1.0 estimates do not exist in any
database** — the recompute never actually completed — and even the database the API
reads has no data at all:

1. **The v1.1.0 recompute never succeeded — it almost certainly crashed.** Your
   local database (`localhost:5433/wced`) is one migration behind: it is at Alembic
   **`001`**, missing **`002_add_publication_log_and_recompute_runs`**. The very
   first thing `wced recompute` does is open a row in the `recompute_runs` table —
   which doesn't exist at `001` — so the command errors out immediately
   (`relation "recompute_runs" does not exist`) before writing anything. The local
   DB has emission estimates only through **v1.0.5** (each version 47 estimates);
   **there is no v1.1.0 anywhere**, and nothing has been written since 2026-05-24.

2. **The deployed API reads a different, empty database.** The API reads a
   **separate Neon database** (`ep-xxxxx…neon.tech/neondb`),
   not your local one. Neon is migrated to `002` but **completely empty** — zero
   facilities, zero events, zero estimates. So even the old numbers aren't there;
   the headline is all zeros. Your 20 facilities / 67 events live only in the local
   DB, which the API never reads.

3. **The version label `1.0.5` is hard-coded in the API code**, not read from the
   database. Even after you load real v1.1.0 data into Neon, the dashboard will
   *still* print "v1.0.5" and the old "…ACLED. Analysis: WCED v1.0.5" attribution
   until that constant is changed in code and redeployed.

Re-running `wced recompute` as-is fixes none of this: against local it crashes
(missing `002`), and against Neon it is a no-op (`wced recompute` only reprocesses
**already-published events**, and Neon has none).

---

## 1. Which databases are in play

| | **Local DB (`.env` / your shell)** | **Neon DB (Modal `wced-secrets`)** |
|---|---|---|
| Host | `localhost:5433` | `ep-xxxxx.neon.tech` |
| Database / user | `wced` / `wced` | `neondb` / `$NEON_USER` |
| Driver in DSN | `postgresql+asyncpg` | `postgresql` (psycopg2) |
| Who uses it | `wced recompute`, `wced detect`, all local CLI work (resolved via `wced/db/session.py` → `DATABASE_URL`) | **The deployed Modal API** (`modal_app.py` → `wced.api.main.create_app` → reads `DATABASE_URL` from the `wced-secrets` secret) |
| Alembic head | **`001`** (missing migration `002`) | `002` |
| Data | 20 facilities, 67 events, estimates v1.0–v1.0.5 | **empty** (0 of everything) |
| Reachable now? | Yes (started for this diagnosis; was down at first) | Yes |

**They are NOT the same database.** The recompute target (local 5433) and the
API's database (Neon) are different servers. Passwords were never printed; only
hosts/usernames were inspected.

> Note: the Modal secret `wced-secrets` was created today (2026-05-30 18:37 IST)
> and is what the live API binds to. The local `.env` was last touched May 24.

---

## 2. State of each database

### Neon — the database the deployed API actually reads (read-only SELECTs via Modal)

- **Alembic head:** `002` (stored in `wced_alembic_version`, the project's custom
  version table). Migration **`002_add_publication_log_and_recompute_runs` IS
  applied** — `publication_log` and `recompute_runs` tables both exist.
- **`fire_events`:** `0` rows
- **`facilities`:** `0` rows
- **`emission_estimates` grouped by `methodology_version`:** `[]` (no estimates of
  any version — no v1.0.5, no v1.1.0, nothing)
- **`recompute_runs`:** `0` rows (latest: none) → **no recompute has ever run
  against this database**
- **`fire_events` by `status`:** `[]` (no events, so nothing PENDING/PUBLISHED)
- **`publication_log`:** exists, `0` rows
- **`methodology_versions`:** `0` rows (this is why `/v1/methodology/current`
  returns 404)
- **Tables present:** all expected tables exist (facilities, fire_events,
  emission_estimates, publication_log, recompute_runs, methodology_versions,
  provenance_records, …). **Schema is complete; data is absent.**

**Conclusion: Neon is migrated but never seeded or run.**

### Local (`localhost:5433/wced`) — the recompute target (started for this diagnosis, read-only)

- **Alembic head:** **`001`** (`wced_alembic_version` = `001`). Migration
  **`002_add_publication_log_and_recompute_runs` is NOT applied** — the
  `recompute_runs` and `publication_log` tables **do not exist** here.
- **`facilities`:** `20` rows
- **`fire_events`:** `67` rows, **all `PUBLISHED`**, across 7 distinct facilities
- **`emission_estimates` by `methodology_version`:** `1.0`, `1.0.1`, `1.0.2`,
  `1.0.3`, `1.0.4`, `1.0.5` — **47 each. There is NO `1.1.0`.** Newest estimate
  timestamp is `2026-05-24 09:14` (v1.0.5); nothing written since.
- **`methodology_versions`:** empty.
- **Why the recompute left no trace:** `wced recompute` opens a `recompute_runs`
  row as its first DB write (`wced/cli/main.py` → `RecomputeRunRepository.open_run`).
  Because that table doesn't exist at head `001`, the command raises
  `relation "recompute_runs" does not exist` and aborts before writing any v1.1.0
  estimates. This is the most likely error you hit when you "ran the recompute."

**Conclusion: the local DB has real data but only through v1.0.5, and it is a
migration behind, so the v1.1.0 recompute could not have completed here.**

---

## 3. Live API cross-check (https://$YOUR_MODAL_URL)

- `GET /v1/health` → `{"status":"ok","version":"0.1.0","database":"ok"}` — the API
  *is* connected to a database (Neon), and that DB answers queries. "database: ok"
  means *reachable*, not *populated*.
- `GET /v1/meta` → `event_count: 0`, `facility_count: 0`, `last_data_update: null`,
  `methodology_version: "1.0.5"`. The counts are read live from Neon (hence 0).
  **The `methodology_version` is NOT read from the DB** — `meta()` never sets it;
  it is the hard-coded default `METHODOLOGY_VERSION = "1.0.5"` in
  `wced/api/schemas/responses.py:10`.
- `GET /v1/aggregates/headline` → all totals `0.0`, `confirmed_event_count: 0`,
  attribution `"…ACLED. Analysis: WCED v1.0.5"` (also the hard-coded
  `ATTRIBUTION` constant, `responses.py:12`).
- `GET /v1/facilities` and `GET /v1/events` → `data: []`, `total: 0`.
- `GET /v1/methodology/current` → `404 No methodology version registered`
  (consistent with `methodology_versions` being empty in Neon).

The API's reported DB state (empty, no registered methodology) matches the direct
read-only SELECTs in §2 exactly. The deployed API reads the empty Neon database.

---

## 4. Answers to the specific questions

- **Which database does the deployed API read?**
  The Neon database `ep-xxxxx.neon.tech/neondb`,
  via the Modal `wced-secrets` secret's `DATABASE_URL`.

- **Do v1.1.0 estimates exist in THAT database?**
  **No.** Neon has **zero** rows in `emission_estimates` — no v1.1.0, no v1.0.5,
  nothing. It has no facilities and no events either. (And v1.1.0 doesn't exist in
  the local DB either — its newest estimates are v1.0.5.)

- **Did the recompute run against a DIFFERENT database than the API reads?**
  **Yes — and it never completed anywhere.** `recompute_runs` is empty in Neon
  (no recompute ever touched it). It was pointed at the local `localhost:5433/wced`
  DB (the `.env` target the API does not read), but there it crashed immediately
  because the local DB is at Alembic `001` and lacks the `recompute_runs` table.
  Net result: no v1.1.0 estimates were produced in either database.

- **Are events stuck in PENDING_REVIEW (recompute ran but nothing approved)?**
  **No.** On Neon there are 0 events. On local, all 67 events are still `PUBLISHED`
  (the recompute that would have routed them to `PENDING_REVIEW` never ran). So the
  symptom is "recompute never happened," not "ran but nothing approved."
  (Heads-up for after you fix this: a *successful* `wced recompute` routes *every*
  recomputed event back to `PENDING_REVIEW` — see
  `wced/pipeline/recompute.py:route_events_to_pending_review` — so you will then
  need to re-approve events before the headline counts them; `headline` only sums
  published/confirmed events.)

---

## 5. Root cause

Three compounding causes, in order of importance:

1. **The v1.1.0 recompute never completed, so v1.1.0 estimates exist nowhere.** It
   was run against the local DB, which is a migration behind (Alembic `001`, missing
   `002`); `wced recompute` aborts on its first write because `recompute_runs`
   doesn't exist. The local DB's newest estimates are v1.0.5.

2. **The deployed API reads a different database from your local one, and that
   database is empty.** The API binds to the Neon DB in `wced-secrets`
   (`…neon.tech/neondb`), which is migrated to `002` but was never seeded — 0
   facilities, 0 events, 0 estimates. Your real data (20 facilities / 67 events)
   lives only in `localhost:5433`, which the API never reads. So the live dashboard
   shows zeros.

3. **The version/attribution label is hard-coded in the API code.** `METHODOLOGY_VERSION`
   and `ATTRIBUTION` are constants in `wced/api/schemas/responses.py:10-12`, used as
   Pydantic defaults for `MetaResponse`/`HeadlineResponse`. They will read `1.0.5` /
   "ACLED" regardless of database contents until the code is changed and redeployed.

To get a correct v1.1.0 dashboard you must address all three: complete a real
v1.1.0 recompute, do it **against the database the API reads (Neon)** with the data
present and migration `002` applied, and fix the hard-coded label.

---

## 6. Fix — commands to review (DO NOT RUN yet; for the user to run)

> Two things must both become true: (a) the data lives in the database the API
> reads (Neon), and (b) a v1.1.0 recompute actually completes there — which
> requires migration `002` to be applied wherever the recompute runs.

The recommended path uses the data you already have locally, gets it onto Neon,
and recomputes on Neon (which is already at `002`).

**Part A — copy your existing local data into Neon.** The local DB has the 20
facilities / 67 events / estimates through v1.0.5; Neon has the schema but no data.

```bash
# DSNs (psycopg2 form). Neon needs sslmode=require.
#   LOCAL_DSN = postgresql://wced:<pwd>@localhost:5433/wced
#   NEON_DSN  = postgresql://$NEON_USER:<pwd>@ep-xxxxx.neon.tech/neondb?sslmode=require

# Local is at Alembic 001, so it has NO publication_log / recompute_runs tables —
# don't try to dump those. Dump only the data tables that exist locally:
pg_dump "$LOCAL_DSN" --data-only --no-owner --no-acl \
    -t facilities -t fire_events -t emission_estimates -t firms_detections \
    -t provenance_records -t provenance_inputs -t sources \
    -t methodology_versions -t editorial_actions -t damage_assessments \
  | psql "$NEON_DSN"
```
(Or use the existing helper `scripts/export_snapshot.py` if it covers these tables.)

**Part B — recompute to v1.1.0 against Neon** (Neon is already at `002`, so the
`recompute_runs`/`publication_log` writes will succeed):

```bash
export DATABASE_URL="postgresql+psycopg2://$NEON_USER:<pwd>@ep-xxxxx.neon.tech/neondb?sslmode=require"
wced recompute --methodology-version 1.1.0      # opens recompute_runs, writes v1.1.0 estimates
```

> Alternative if you'd rather validate locally first: upgrade the **local** DB to
> `002` (`wced db migrate --yes` / `alembic upgrade head` against `localhost:5433`),
> run `wced recompute --methodology-version 1.1.0` there to confirm it produces
> v1.1.0 estimates, then dump local→Neon (now including `recompute_runs` /
> `publication_log` / the new estimates) instead of recomputing on Neon.

**Part C — approve so the headline counts them** (a successful recompute routes
every event to `PENDING_REVIEW`):

```bash
# still pointed at NEON_DSN
wced verify list --status PENDING_REVIEW
wced verify approve <event_id> --note "v1.1.0 republish"     # per event
# (scripts/approve_top_events.sh batches this if appropriate)
```

**Part D — fix the hard-coded version/attribution label, then redeploy.** Edit
`wced/api/schemas/responses.py:10-12`:

```python
METHODOLOGY_VERSION = "1.1.0"
ATTRIBUTION = "Data: NASA FIRMS, ESA Copernicus, GDELT. Analysis: WCED v1.1.0"
```
(Better long-term: source `methodology_version` from the `methodology_versions`
table so it can never drift from the data again.) Then redeploy the API:

```bash
modal deploy modal_app.py
```

**Verify the fix (read-only):**

```bash
curl -s https://$YOUR_MODAL_URL/v1/meta
curl -s https://$YOUR_MODAL_URL/v1/aggregates/headline
```
Expect non-zero `event_count`/`facility_count`, non-zero headline totals, and
`methodology_version: "1.1.0"`.

---

*Investigation artifacts: read-only Modal probes were used to read the Neon secret
host and run `SELECT`-only queries; no schema or data was modified. Passwords were
never printed.*
