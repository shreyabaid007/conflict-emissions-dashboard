# V2 Gap Report

> Comparing the current codebase (commit `741726d`, branch `phase0-audit-cleanup`) against the v2 execution plan (`docs/WCED_execution_plan_v2.md`).
> Generated 2026-05-30.

---

## How to read this report

Each gap entry contains:
- **File(s):** path(s) affected
- **Current state:** what exists today
- **Required change:** what v2 demands
- **Task type:** `new-file` | `edit` | `delete`
- **Blast radius / risk:** what breaks or needs retesting if this changes

Items marked **⚠ V1-POLICY CONFLICT** encode v1 decisions that directly contradict the v2 plan and must be resolved first.

---

## Stage 0 — Decision + Safety Rails + Free Infra (this week)

### 0.1 ⚠ V1-POLICY CONFLICT — "No auto-publish for 6 months" rule

- **File(s):** `CLAUDE.md:95`, `Justfile:55`
- **Current state:** CLAUDE.md anti-pattern list says _"Auto-publishing incidents to the dashboard without editorial review for the first 6 months"_. Justfile hardcodes `--no-auto-publish` on the `detect` recipe.
- **Required change:** Replace the blanket ban with a **confidence-gated auto-publish policy** (v2 §11). `Confirmed`/`Verified` events auto-publish; `Reported`/`Suspected`/`Claimed` route to a hold queue. Update CLAUDE.md, Justfile, and the editorial module.
- **Task type:** `edit`
- **Blast radius / risk:** HIGH — this is the foundational v2 policy change. Every downstream auto-publish feature depends on it. Requires updating `wced/verify/editorial.py`, `wced/models/editorial.py`, `wced/cli/main.py`, and adding confidence-gated publish logic. Must be accompanied by the safety rails in 0.2–0.5 before going live.

### 0.2 Missing — Publication log (append-only audit table)

- **File(s):** `wced/db/models.py`, `wced/db/migrations/versions/`
- **Current state:** No `publication_log` table exists. The v2 plan (§2) requires an append-only log of every publish/retract/restate transition.
- **Required change:** Add `publication_log` table to ORM and migration. Every call to `wced verify approve/reject/retract` must append a record.
- **Task type:** `new-file` (migration), `edit` (models.py, editorial.py)
- **Blast radius / risk:** MEDIUM — new table, no existing data affected. But the editorial workflow and CLI commands must be updated to write to it.

### 0.3 Missing — Recompute runs table

- **File(s):** `wced/db/models.py`, `wced/db/migrations/versions/`
- **Current state:** No `recompute_runs` table. v2 §2 lists it as a required audit table.
- **Required change:** Add table tracking each `wced recompute` invocation (methodology version, date range, initiator, result count).
- **Task type:** `new-file` (migration), `edit` (models.py, cli/main.py)
- **Blast radius / risk:** LOW — additive.

### 0.4 ⚠ V1-POLICY CONFLICT — ACLED still wired as primary corroboration source

- **File(s):**
  - `wced/ingest/acled.py` — full ACLED connector (OAuth, data fetch)
  - `wced/verify/acled_corroboration.py` — backward-compat shim
  - `wced/verify/corroboration.py` — imports `ACLEDEvent`, treats ACLED as "strong" corroboration
  - `wced/verify/confidence.py` — ACLED match → CONFIRMED; GDELT match → caps at VERIFIED
  - `wced/pipeline/daily_ingest.py` — step 4 is `ingest_acled`, step 8 is `corroborate_with_acled`
  - `CLAUDE.md:49,51` — lists ACLED in repo structure
  - `.steering/structure.md:60,73,102,200` — lists `acled.py`, `acled_corroboration.py`, "FIRMS + ACLED ingest", "VCR cassettes for ACLED"
  - `.steering/product.md:37` — "Not a casualty tracker — out of scope, see ACLED"
  - `tests/fixtures/cassettes/` — ACLED VCR cassettes
- **Current state:** ACLED is deeply integrated as the human-reviewed conflict-event source. The confidence decision table (§4.3, Table 5) reserves CONFIRMED for ACLED-corroborated events. GDELT is treated as secondary (caps at VERIFIED).
- **Required change:** v2 drops ACLED (no longer free). GDELT becomes primary conflict-event source; UCDP added for historical validation. The confidence decision table must be revised: GDELT needs to be promoted from "caps at VERIFIED" to a stronger role, or the corroboration rule must rely on GDELT + satellite confirmation instead. The ACLED connector should be retained behind a feature flag (for future funded access) rather than deleted.
- **Task type:** `edit` (confidence.py, corroboration.py, daily_ingest.py, CLAUDE.md, steering files), `new-file` (UCDP connector), optional `delete` or feature-flag (`acled.py`, `acled_corroboration.py`)
- **Blast radius / risk:** HIGH — touches the confidence label logic, the pipeline, and the methodology. Requires a methodology re-version (v1.1.0) per v2 caveats. All existing test fixtures and cassettes referencing ACLED need updating.

### 0.5 Missing — UCDP connector for historical validation

- **File(s):** `wced/ingest/ucdp.py` (does not exist)
- **Current state:** No UCDP module. v2 §3 requires it for historical validation/backfill.
- **Required change:** Implement `wced/ingest/ucdp.py` implementing `IngestConnector` protocol. Add to `.steering/structure.md`.
- **Task type:** `new-file`
- **Blast radius / risk:** LOW — additive, no existing code affected.

### 0.6 Missing — Modal.com deployment

- **File(s):** No Modal files exist anywhere in repo.
- **Current state:** Deployment is Docker Compose (dev) + Helm charts placeholder (prod). `tech.md:143` targets "< $500/mo (small VPS + S3 + PG instance)".
- **Required change:** v2 §9 requires Modal for serverless compute (daily cron + `@modal.asgi_app` for FastAPI). Add `modal_app.py` (or similar) with `modal.Cron` schedule and ASGI wrapper. Update `deploy/` docs.
- **Task type:** `new-file`
- **Blast radius / risk:** MEDIUM — new deployment target; existing Docker Compose stays for local dev. Needs `DATABASE_URL` pointed at external Neon/Supabase.

### 0.7 ⚠ V1-POLICY CONFLICT — Cost targets assume VPS + paid API

- **File(s):** `.steering/tech.md:143-145`
- **Current state:** Cost targets are "< $500/mo infra (small VPS + S3 + PG instance)" and "< $1,000/mo Claude API".
- **Required change:** v2 targets $0/mo additional cash. Infra moves to Modal credits ($1,000), free Neon/Supabase PostGIS, Vercel/Cloudflare Pages. Claude usage covered by Max 5x plan. Update tech.md cost section.
- **Task type:** `edit`
- **Blast radius / risk:** LOW — documentation only, but guides all future infra decisions.

### 0.8 Missing — Free database tier (Neon/Supabase)

- **File(s):** `deploy/docker-compose.yml`, `wced/settings.py`
- **Current state:** PostgreSQL runs in Docker Compose locally. No cloud database provisioned.
- **Required change:** Document Neon/Supabase setup; ensure `DATABASE_URL` env var is configurable for external PostGIS (it likely already is via `wced/settings.py`). Add setup instructions to `docs/DEV_SETUP.md`.
- **Task type:** `edit` (docs)
- **Blast radius / risk:** LOW — settings likely already support external URLs.

### 0.9 Missing — Frontend deployment (Vercel/Cloudflare Pages)

- **File(s):** `frontend/`
- **Current state:** Frontend has Dockerfile (`deploy/Dockerfile.frontend`) for container deployment. No Vercel/Cloudflare config.
- **Required change:** Add `vercel.json` or equivalent; document deployment to free tier.
- **Task type:** `new-file` (config), `edit` (docs)
- **Blast radius / risk:** LOW.

---

## Stage 1 — Plugin Refactor + Auto-Publish (weeks 2–4)

### 1.1 Missing — Emission-category plugin architecture

- **File(s):** `wced/categories/` (does not exist)
- **Current state:** Detect/quantify logic lives directly in `wced/detect/` and `wced/quantify/`. No `EmissionCategory` protocol. No entry-point registration.
- **Required change:** v2 §1 requires `wced/categories/base.py` with `EmissionCategory` protocol (id, methodology_version, detect, verify, quantify, required_sources). Refactor existing oil/fuel fire logic into `wced/categories/oil_fuel_fire/`. Register via `pyproject.toml [project.entry-points."wced.categories"]`.
- **Task type:** `new-file` (categories/base.py, categories/oil_fuel_fire/), `edit` (pyproject.toml, pipeline modules)
- **Blast radius / risk:** HIGH — major refactor touching detect, quantify, verify, and pipeline modules. All tests must be updated. This is the largest structural change in the v2 plan.

### 1.2 Missing — Confidence-gated publication gate

- **File(s):** `wced/verify/editorial.py`, `wced/pipeline/daily_ingest.py`
- **Current state:** All detections enter `PENDING_REVIEW`. No auto-publish path.
- **Required change:** Implement publish function that auto-publishes `Confirmed`/`Verified` events and routes others to hold queue. Reject any estimate missing complete `ProvenanceRecord` chain or `Distribution` with <10,000 samples.
- **Task type:** `edit`
- **Blast radius / risk:** HIGH — changes the editorial workflow. Must be gated behind the Stage 0 policy change.

### 1.3 Missing — Cross-method reconciliation gate

- **File(s):** `wced/quantify/reconcile.py`
- **Current state:** Reconciliation module exists but does not gate publication. v2 §5/§11 requires divergence beyond tolerance to route to review instead of auto-publishing.
- **Required change:** Wire reconciliation result into the publication gate: large bottom-up vs top-down divergence → hold queue.
- **Task type:** `edit`
- **Blast radius / risk:** MEDIUM — needs integration with editorial workflow.

### 1.4 Missing — Anomaly-watch agent

- **File(s):** None exist.
- **Current state:** No anomaly detection on published estimates.
- **Required change:** v2 §8/§11 requires an `anomaly-watch` agent that auto-retracts outlier estimates to `PENDING_REVIEW` with a public "under review" note.
- **Task type:** `new-file`
- **Blast radius / risk:** MEDIUM — writes to editorial state; needs careful testing to avoid false retractions.

### 1.5 Missing — Public revision log

- **File(s):** `wced/api/routes/`, `frontend/`
- **Current state:** Frontend has a `changelog/page.tsx` but no API endpoint serving a public revision/retraction log from the database.
- **Required change:** Add API endpoint for revision history; update frontend to display retractions/restatements (never silently deleted).
- **Task type:** `edit` (API routes), `edit` (frontend)
- **Blast radius / risk:** LOW-MEDIUM — additive API, but frontend changes need testing.

---

## Stage 2 — Run-time Company + Research Loop (weeks 5–8)

### 2.1 Missing — Paperclip agent orchestration

- **File(s):** No Paperclip config exists.
- **Current state:** No agent orchestration layer.
- **Required change:** v2 §9 requires Paperclip org chart with `claude_local` adapter, budget caps, heartbeats. Add config files and `AGENTS.md`.
- **Task type:** `new-file`
- **Blast radius / risk:** LOW — external tool config, no code changes.

### 2.2 Missing — Hermes build-time orchestration

- **File(s):** No `~/.hermes/` config.
- **Current state:** No Hermes integration.
- **Required change:** Configure Hermes for spec-driven development against `.steering/`. Used for build-time Claude Code orchestration.
- **Task type:** `new-file` (config)
- **Blast radius / risk:** LOW — tooling config, no repo code changes.

### 2.3 Missing — Literature-monitor agent

- **File(s):** None exist.
- **Current state:** No automated literature monitoring.
- **Required change:** v2 §7/§8 requires a weekly agent that watches Semantic Scholar/arXiv for new FRP/emission-factor papers, verifies DOIs, opens issues. Never fabricate citations.
- **Task type:** `new-file`
- **Blast radius / risk:** LOW — additive, issues-only output.

### 2.4 Missing — Methodology-drift agent

- **File(s):** None exist.
- **Current state:** No automated methodology drift detection.
- **Required change:** v2 §7/§8 requires an agent that diffs new literature against `methodology/v1.0.5.tex` and proposes (never commits) versioned changes.
- **Task type:** `new-file`
- **Blast radius / risk:** LOW — propose-only, human approves.

### 2.5 Missing — Zenodo DOI integration

- **File(s):** None exist.
- **Current state:** No Zenodo connection. No DOI minting.
- **Required change:** v2 §7 requires connecting repo to Zenodo so each tagged release mints a versioned DOI under one concept DOI. Add `.zenodo.json` and GitHub webhook config.
- **Task type:** `new-file`
- **Blast radius / risk:** LOW — external service config.

### 2.6 GDELT confidence promotion

- **File(s):** `wced/verify/confidence.py`, `wced/ingest/gdelt.py`
- **Current state:** GDELT is explicitly capped: _"GDELT corroboration can never push an event above REPORTED on its own"_ (gdelt.py docstring) and _"GDELT match only → caps at VERIFIED"_ (confidence.py). This made sense when ACLED was primary.
- **Required change:** With ACLED dropped, GDELT becomes the primary conflict-event source. The confidence table must be revised so GDELT + satellite confirmation can reach CONFIRMED (per v2's "≥2 independent sources OR satellite confirmation" rule). Update docstrings in both files. Methodology re-version required.
- **Task type:** `edit`
- **Blast radius / risk:** HIGH — changes the confidence label semantics. Every existing event's confidence label was computed under the old rules. Needs careful migration strategy (recompute or grandfather).

---

## Stage 3 — Scale (quarter 2)

### 3.1 Missing — Second emission category plugin

- **File(s):** `wced/categories/` (does not exist yet)
- **Current state:** Only oil/fuel fires implemented.
- **Required change:** Add destroyed-buildings category as second plugin. Requires its own methodology version, tests, and data sources.
- **Task type:** `new-file`
- **Blast radius / risk:** LOW if plugin architecture (1.1) is in place — that's the whole point of the plugin design.

### 3.2 Missing — Draft paper from pipeline outputs

- **File(s):** `notebooks/`
- **Current state:** No reproducible paper-drafting workflow.
- **Required change:** v2 §7 requires reproducible notebooks with pinned methodology version and Monte-Carlo seed. Agents draft prose; human verifies.
- **Task type:** `new-file` (notebooks + drafting workflow)
- **Blast radius / risk:** LOW — additive.

---

## Cross-cutting: V1-era references in steering/config files

These items don't map to a single stage but must be updated to avoid contradicting v2 decisions.

### C.1 ⚠ CLAUDE.md — ACLED references

- **File:** `CLAUDE.md`
- **Lines:** 49 (`wced/ingest/` lists `acled`), 51 (`wced/verify/` lists `acled_corroboration`)
- **Required change:** Update repo structure description. Replace ACLED with GDELT as primary; add UCDP. Note ACLED is retained behind feature flag.
- **Task type:** `edit`

### C.2 ⚠ `.steering/structure.md` — ACLED references

- **File:** `.steering/structure.md`
- **Lines:** 60, 73, 102, 200
- **Required change:** Update to reflect GDELT as primary, ACLED behind feature flag, UCDP for historical. Update `daily_ingest.py` description from "FIRMS + ACLED" to "FIRMS + GDELT". Update cassettes reference.
- **Task type:** `edit`

### C.3 ⚠ `.steering/product.md` — ACLED reference

- **File:** `.steering/product.md`
- **Line:** 37
- **Current state:** "Not a casualty tracker — out of scope, see ACLED"
- **Required change:** Remove or replace ACLED reference. ACLED is still relevant as a casualty-tracking project but the reference implies we use it.
- **Task type:** `edit` (minor wording)

### C.4 ⚠ `.steering/tech.md` — Cost targets

- **File:** `.steering/tech.md`
- **Lines:** 143-147
- **Current state:** Infrastructure < $500/mo, Claude API < $1,000/mo, Planet Labs budget.
- **Required change:** Update to $0/mo additional (Modal credits, free Neon/Supabase, Vercel/Cloudflare, Claude Max 5x).
- **Task type:** `edit`

### C.5 ⚠ `.steering/tech.md` — Missing Modal/Neon/Vercel in stack

- **File:** `.steering/tech.md`
- **Current state:** No mention of Modal, Neon, Supabase, Vercel, or Cloudflare Pages.
- **Required change:** Add deployment section covering serverless compute (Modal), managed PostGIS (Neon/Supabase), and static frontend hosting (Vercel/Cloudflare).
- **Task type:** `edit`

### C.6 ⚠ CLAUDE.md — Missing methodology re-version note

- **File:** `CLAUDE.md`
- **Lines:** 107-112
- **Current state:** Latest version listed as v1.0.5. No mention of pending v1.1.0 for the ACLED→GDELT source swap.
- **Required change:** After the ACLED removal is implemented, update methodology versioning section to include v1.1.0 and rationale.
- **Task type:** `edit` (after implementation)

### C.7 `wced/pipeline/daily_ingest.py` — ACLED pipeline steps

- **File:** `wced/pipeline/daily_ingest.py`
- **Lines:** ~4 (step 4: `ingest_acled`), ~8 (step 8: `corroborate_with_acled`)
- **Current state:** Pipeline is a fixed 11-step sequence with ACLED hardwired as steps 4 and 8.
- **Required change:** Replace `ingest_acled` with `ingest_gdelt` (or make source configurable). Replace `corroborate_with_acled` with `corroborate_with_conflict_events` (source-agnostic).
- **Task type:** `edit`
- **Blast radius / risk:** HIGH — changes the core pipeline. All pipeline integration tests need updating.

### C.8 Missing — `/provenance/{id}` standalone route

- **File(s):** `wced/api/routes/`
- **Current state:** Provenance is available as a sub-route on events (`/events/{id}/provenance`) but v2 §6 requires a standalone `/provenance/{id}` endpoint so every number is click-through-auditable from the dashboard.
- **Required change:** Add `wced/api/routes/provenance.py` with `GET /provenance/{id}` returning full source chain.
- **Task type:** `new-file` or `edit`
- **Blast radius / risk:** LOW — additive API endpoint.

---

## Summary matrix

| Stage | Gap ID | Priority | Task type | Risk |
|-------|--------|----------|-----------|------|
| 0 | 0.1 Auto-publish policy | P0 | edit | HIGH |
| 0 | 0.2 Publication log table | P0 | new + edit | MEDIUM |
| 0 | 0.3 Recompute runs table | P1 | new + edit | LOW |
| 0 | 0.4 ACLED removal / GDELT promotion | P0 | edit + new | HIGH |
| 0 | 0.5 UCDP connector | P1 | new | LOW |
| 0 | 0.6 Modal deployment | P0 | new | MEDIUM |
| 0 | 0.7 Cost targets update | P1 | edit | LOW |
| 0 | 0.8 Neon/Supabase setup | P1 | edit (docs) | LOW |
| 0 | 0.9 Frontend deployment | P2 | new + edit | LOW |
| 1 | 1.1 Plugin architecture | P0 | new + edit | HIGH |
| 1 | 1.2 Confidence-gated publish | P0 | edit | HIGH |
| 1 | 1.3 Cross-method gate | P1 | edit | MEDIUM |
| 1 | 1.4 Anomaly-watch agent | P1 | new | MEDIUM |
| 1 | 1.5 Public revision log | P2 | edit | LOW-MED |
| 2 | 2.1 Paperclip config | P2 | new | LOW |
| 2 | 2.2 Hermes config | P2 | new | LOW |
| 2 | 2.3 Literature monitor | P2 | new | LOW |
| 2 | 2.4 Methodology-drift agent | P2 | new | LOW |
| 2 | 2.5 Zenodo DOI | P2 | new | LOW |
| 2 | 2.6 GDELT confidence promotion | P0 | edit | HIGH |
| 3 | 3.1 Second category plugin | P3 | new | LOW |
| 3 | 3.2 Paper drafting workflow | P3 | new | LOW |
| — | C.1–C.6 Steering/config updates | P1 | edit | LOW |
| — | C.7 Pipeline ACLED steps | P0 | edit | HIGH |
| — | C.8 Standalone provenance route | P2 | new/edit | LOW |

**Critical path:** 0.1 (policy) → 0.4 + 2.6 (ACLED→GDELT, methodology re-version) → 1.1 (plugin refactor) → 1.2 (auto-publish gate) → 0.6 (Modal deploy).

---

## Items NOT gapped (already in place)

These v2 requirements are already satisfied by the current codebase:

- **ProvenanceRecord model** — exists in `wced/models/provenance.py`
- **Distribution model** — exists in `wced/quantify/distribution.py` with p5/p50/p95/samples/provenance
- **Editorial state machine** — exists in `wced/models/editorial.py` (PENDING_REVIEW → PUBLISHED/REJECTED → RETRACTED)
- **methodology_versions table** — exists in `wced/db/models.py` and migration
- **GDELT connector** — exists in `wced/ingest/gdelt.py` (DOC API + Events flat files)
- **Sentinel-2 SWIR check** — exists in `wced/verify/sentinel2_check.py`
- **Sentinel-5P/TROPOMI** — exists in `wced/ingest/sentinel5p.py` + `wced/validate/tropomi.py`
- **Source-agnostic corroboration** — exists in `wced/verify/corroboration.py` (supports both ACLED and GDELT)
- **Monte Carlo distribution** — exists in `wced/quantify/distribution.py`
- **FRP + inventory dual estimation** — exists in `wced/quantify/frp.py` and `wced/quantify/inventory.py`
- **Reconciliation module** — exists in `wced/quantify/reconcile.py`
- **CLI editorial commands** — exist in `wced/cli/verify.py`
- **Methodology versioning on estimates** — enforced in DB schema
- **Frontend with MapLibre + p5/p50/p95 display** — exists in `frontend/`
- **Facility registry** — exists in `data/facilities/`
- **Emission factors in YAML** — exists in `data/emission_factors.yaml`
- **structlog + OpenTelemetry** — exists in `wced/logging.py` and `wced/api/middleware/telemetry.py`
- **IngestConnector protocol** — exists in `wced/ingest/base.py`
- **Provenance sub-route on events** — exists at `/events/{id}/provenance`
