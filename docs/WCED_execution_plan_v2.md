# WCED Execution Plan v2 — Building a Production-Grade Conflict-Emissions Dashboard with a Coordinated AI Agent System

> **Revision note (v2).** Three changes from v1, per founder feedback:
> 1. **ACLED dropped** — it is no longer free, so it is out of the V1 stack. **GDELT** (free, global) becomes the primary conflict-event source; **UCDP** (free, academic) handles historical validation. Methodology is unaffected because FIRMS + Sentinel-2 satellite confirmation already satisfies the corroboration rule.
> 2. **Zero additional cash.** The plan now targets **$0/mo new spend** beyond the Claude Max plan already owned.
> 3. **Hosting on Modal.com credits.** The ~$1,000 in Modal credits funds all compute (serverless cron + FastAPI, scale-to-zero); the database moves to a **free Neon/Supabase PostGIS tier**; the frontend to **Vercel/Cloudflare Pages free**. Hetzner is no longer needed at MVP.

---

## TL;DR
- **Build on the existing repo, don't restart.** `conflict-emissions-dashboard` already has the right skeleton (FastAPI + Pydantic v2 + PostGIS/GeoAlchemy2 + Next.js/MapLibre + Prefect-style pipeline + `.steering/` specs + CLAUDE.md + versioned methodology v1.0.5). The job is to harden it into a plugin-based, auto-publishing system and wrap it in a three-tool agent system where **Hermes drives build-time code generation, Paperclip runs run-time operations as a "company," and Claude Code (on your Max plan) is the underlying worker.**
- **Run it for $0 additional cash.** You already have **Claude Max 5x ($100/mo)** — that covers all agent work. Host compute on your **$1,000 Modal.com credits** (serverless cron + FastAPI, scales to zero between runs), put the database on a **free Neon or Supabase PostGIS tier**, and the frontend on **Vercel/Cloudflare Pages free**. No new paid services. **ACLED is dropped (no longer free); GDELT replaces it** with no methodology loss.
- **The model-(b) auto-publish decision is defensible only if confidence-gating + provenance + Monte-Carlo uncertainty + a post-hoc review queue + one-command rollback are enforced in code, not in discipline.** Auto-publish only `Confirmed`/`Verified` incidents; route `Reported`/`Suspected` to a hold queue; every published number carries a `ProvenanceRecord` and a 5th/50th/95th distribution; anomalies auto-retract to `PENDING_REVIEW`.
- **Cost and safety are controlled at the orchestration layer, not the model.** Cap every Paperclip agent with `budgetMonthlyCents`, use `claude -p --max-turns` for autonomous runs, and run **single-agent passes** on Max 5x (avoid parallel agent teams / aggressive heartbeats that blow the weekly rate limit).

---

## Key Findings

1. **The repo is further along than a greenfield.** It encodes the four non-negotiables (provenance, uncertainty, AI-never-final, version-everything) in `CLAUDE.md`, has a live headline number (~75.6 kt CO₂e p50 across 7 Iranian facilities, 47 events as of 2026-05-24), a methodology at v1.0.5, an editorial state machine (`PENDING_REVIEW → PUBLISHED/REJECTED → RETRACTED`), and a `wced` Typer CLI. **CLAUDE.md currently forbids auto-publishing for the first 6 months** — your model-(b) decision contradicts this, so the first code change is to replace that with a confidence-gated auto-publish policy.
2. **Spec-driven development (SDD) is the dominant 2026 pattern** and the repo is set up for it via `.steering/`. GitHub Spec Kit formalizes `/specify → /plan → /tasks → /implement` and is the correct backbone for Hermes/Claude Code build-time work.
3. **Harness + eval-driven development is how fast teams stay accurate.** Agent = Model + Harness, with computational sensors (linters, type checkers, test suites) enforcing correctness near 100% versus inferential (LLM) controls. For WCED, the deterministic methodology tests **are** the harness — more trustworthy than any judge agent.
4. **Top-down FRP→emissions and bottom-up inventory×factor are both peer-reviewed and should be cross-validated.** Wooster et al. (2005, doi:10.1029/2005JD006318) established FRE↔biomass-combusted with FBCC ≈ 0.368 kg/MJ (literature range 0.368–0.453). EPA's Greenhouse Gas Equivalencies Calculator gives crude ≈ 0.43 t CO₂/barrel. Both methods are already encoded in `quantify/`.
5. **Solo-founder AI-agent operations are real but failure-prone.** Winners build verification into workflows and treat agents as staff, not tools — matching WCED's "AI never final authority" principle.
6. **Free data sources fully cover V1.** With ACLED removed, the stack relies on FIRMS, Sentinel-2/5P (Planetary Computer), GEM, OSM, GDELT, and UCDP — all free.
7. **Two tool-definition specifics could not be verified against primary sources** ("Nightshift" and the "0.9 rollback threshold") — flagged in Caveats; the plan uses only verified mechanisms.

---

## Details

### 1. Project architecture (building on the existing repo)

**Verdict: keep the existing layout; add a plugin layer and an auto-publish/audit layer.** Canonical structure:

```
wced/
  ingest/      firms, gdelt, sentinel2, sentinel5p, ucdp
  detect/      hotspot, facility_match, baseline, persistence
  verify/      sentinel2_check, gdelt_corroboration, confidence, editorial
  quantify/    frp, inventory, factors, aggregate, reconcile, distribution
  ai/          claude client wrapper + vision classify
  provenance/  provenance store
  pipeline/    daily_ingest, quantification, validation_weekly
  api/         FastAPI routes (events, facilities, aggregates, timeseries, meta)
  db/          SQLAlchemy models, Alembic migrations, repositories
  categories/  base.py (EmissionCategory protocol) + oil_fuel_fire/
  cli/         Typer CLI (main.py, verify.py)
data/  methodology/  tests/  deploy/  scripts/  frontend/  notebooks/
```

**Add the emission-category plugin architecture.** `wced/categories/` with a registry so each future category (oil/fuel fires = V1; destroyed buildings, combat fuel, munitions embodied carbon, shipping rerouting, reconstruction projections = V2+) is a self-contained module implementing a common protocol:

```python
# wced/categories/base.py
class EmissionCategory(Protocol):
    id: str                      # "oil_fuel_fire"
    methodology_version: str     # "1.0.5"
    def detect(self, ctx: PipelineCtx) -> list[DetectionEvent]: ...
    def verify(self, ev: DetectionEvent) -> VerificationResult: ...
    def quantify(self, ev: VerifiedEvent) -> Distribution: ...   # p5/p50/p95 + provenance
    def required_sources(self) -> list[SourceSpec]: ...
```

Register via entry points (`pyproject.toml [project.entry-points."wced.categories"]`). V1 refactors existing `detect/quantify` into `categories/oil_fuel_fire/`. **Tech choices (confirming repo picks):** Python 3.11+, FastAPI, Pydantic v2, GeoAlchemy2/PostGIS, `pystac-client`+`planetary-computer`, NumPy/SciPy, `httpx`+`tenacity`, `structlog`, Typer, Justfile, `uv`. Frontend stays Next.js + MapLibre.

### 2. Data pipeline design & provenance schema

Layers: **Ingestion → Detection → Verification → Quantification → Uncertainty → Publication.** Typed Pydantic records cross every boundary; nothing passes as a bare dict.

```python
class ProvenanceRecord(BaseModel):
    id: UUID
    created_at: datetime
    source_type: Literal["satellite","emission_factor","facility_registry",
                          "conflict_event","ai_classification","derived"]
    source_ref: str            # FIRMS granule ID / DOI / GEM unit ID / GDELT GKG ID
    source_license: str        # "public domain" | "CC-BY" | "ODbL"
    methodology_version: str
    parent_ids: list[UUID]
    transform: str             # function name + git commit SHA
    ai_involved: bool
    ai_model: str | None
    confidence: Literal["Confirmed","Verified","Reported","Suspected","Claimed"]

class Distribution(BaseModel):
    p5: float; p50: float; p95: float
    samples: list[float]       # >=10,000 Monte Carlo draws
    unit: str                  # "t CO2e"
    provenance_id: UUID
```

Every emission function returns a `Distribution`, never a float. **Audit tables:** `events`, `facilities`, `estimates`, `provenance`, `methodology_versions`, `publication_log` (append-only), `recompute_runs`. Cache keys include `methodology_version`.

### 3. Real-time event ingestion

- **FIRMS connector** (`ingest/firms.py`): poll the Area API for VIIRS S-NPP/NOAA-20/21 + MODIS over the Iran/Gulf bbox. Honor the **5,000 transactions / 10-minute** rate limit. Schedule daily (3-hour latency — never claim "real-time"). `httpx` async + `tenacity` retry; persist granule IDs to provenance, not rasters.
- **GDELT connector** (`ingest/gdelt.py`) — **primary conflict-event source**: GDELT is free and global. Use it for conflict-event cross-reference, confidence labeling, and the "≥2 independent sources" rule. Store official belligerent statements as `Claimed` confidence, never `Confirmed`. Snapshot every pull with a timestamp for reproducibility.
- **UCDP** (`ingest/ucdp.py`) — free, academic: the Uppsala Conflict Data Program georeferenced event dataset for **historical validation/backfill** (periodic updates, not daily).
- **ACLED — DROPPED.** ACLED is no longer free (paid licensing), so it is out of the V1 stack to keep cost at zero. GDELT + satellite confirmation (FIRMS + Sentinel-2 SWIR) already satisfy the ≥2-source / satellite-confirmation rule, so the methodology is unaffected. If you later obtain funded/academic ACLED access, re-add it as an optional corroboration connector behind a feature flag.
- **Scheduling/persistence:** Modal cron (see §9) triggers the daily ingest; idempotent upserts keyed by source granule/event ID.

### 4. FIRMS + Sentinel integration & facility registry

- **Sentinel-2 SWIR check** (`verify/sentinel2_check.py`): `pystac-client` + `planetary-computer` against the Planetary Computer STAC; pull SWIR bands (B11/B12) to confirm an active burn and discriminate fire vs. false positive. Free; sign asset hrefs with the planetary-computer SDK.
- **Sentinel-5P/TROPOMI** (`ingest/sentinel5p.py`): NO₂/CO/SO₂ columns as top-down plume corroboration. Pull L2 NetCDF from Planetary Computer/Copernicus (one product per request).
- **Facility registry** (`scripts/bootstrap_facilities`): Global Energy Monitor trackers (CC-BY 4.0) + OpenStreetMap Overpass for refineries/depots/pipelines/storage/petrochemical. Keep hotspots **within 500 m** of a known facility. Apply the dual-use review checklist before storing sub-100m coordinates.

### 5. Emissions estimation workflow

- **Bottom-up** (`quantify/inventory.py` + `factors.py`): fuel inventory × emission factor. **Crude ≈ 0.43 t CO₂/barrel** (EPA: 5.80 mmbtu/bbl × 20.31 kg C/mmbtu × 44/12); refined products ~0.39–0.43 t/bbl. Per EPA's current calculator, distillate fuel oil = 431.87 kg CO₂/42-gal barrel, LPG = 236.0 kg CO₂/42-gal barrel (use current values, pin source year in `data/emission_factors.yaml`).
- **Top-down** (`quantify/frp.py`): integrate FRP over fire duration → FRE; combustion = FRE × FBCC (0.368 kg/MJ, range 0.368–0.453); ~3.15 kg CO₂ per kg crude burned.
- **Reconcile** (`quantify/reconcile.py`): compare the two; large divergence flags the event for the post-hoc queue instead of auto-publishing.
- **Monte Carlo** (`quantify/distribution.py`): ≥10,000 NumPy draws with explicit parameter PDFs → p5/p50/p95. Implausibly narrow bounds are a red flag.

### 6. Dashboard architecture

- **Frontend**: **Next.js + MapLibre GL JS** (open-source, no Mapbox fees), with **deck.gl overlaid on MapLibre** (`MapboxOverlay`, interleaved) for 100k+ points. Components: live map (facility markers + events colored by confidence), timeline scrubber, cumulative-emissions chart with p5–p95 bands (never a bare p50), alerts/incident feed, methodology/provenance drawer.
- **Backend**: existing FastAPI app, served via Modal (`@modal.asgi_app`). Add `/provenance/{id}` so every number is click-through-auditable.
- **Displaying auto-published numbers**: each shows a confidence badge, "last updated" timestamp, the p5/p50/p95 triple, and a provenance link. Visible **"under review"** state for held items, and a public **revision log** (retractions/restatements shown, never silently deleted).
- **Observability**: structlog JSON logs carrying `event_id, facility_id, source, confidence`; lightweight metrics endpoint.

### 7. Research & paper-writing workflow

- **Literature monitoring** (weekly agent): watches Semantic Scholar/arXiv for new FRP/emission-factor/conflict-emissions papers; opens an issue when a parameter or method might change. **Never let an agent invent citations** — fabricated references in papers rose sharply through 2025–2026; every proposed citation must be verified against a real DOI before entering `methodology/` or a paper.
- **Methodology audit**: a `methodology-drift` agent diffs new literature against `methodology/v1.0.5.tex` and proposes (never commits) a versioned change; you (the Scientific Steering Committee) approve before any `wced recompute --methodology-version`.
- **Reproducible notebooks** (`notebooks/`): every figure regenerated from a git-tracked snapshot, with pinned methodology version and Monte-Carlo seed for byte-reproducibility.
- **DOI/Zenodo versioning**: connect the repo to Zenodo so each tagged release (methodology version + data snapshot) mints a versioned DOI under one concept DOI — satisfying FAIR (a GitHub URL is not a persistent identifier; the DOI is).
- **Drafting with human as scientific authority**: agents draft methods/results prose from actual pipeline outputs; you remain the named author and verify every number and citation.

### 8. Agent roster & task decomposition

| Agent | Type | Input → Output contract | Notes |
|---|---|---|---|
| `facility-discovery` | run-time | new GEM/OSM deltas → candidate `Facility` records | human approves before registry insert |
| `flaring-discrimination` | run-time | hotspot + S2 SWIR + S5P → `{class, confidence}` | AI output paired with provenance; never final |
| `provenance-tracer` | run-time | `estimate_id` → full source-chain audit | deterministic; flags missing provenance |
| `verification` | run-time | detection event → `VerificationResult` + confidence | enforces ≥2 sources / satellite confirm (GDELT + satellite) |
| `methodology-drift` | run-time (weekly) | new papers → proposed methodology diff | proposes only; you approve |
| `literature-monitor` | run-time (weekly) | search feeds → issues with verified DOIs | citation verification mandatory |
| `anomaly-watch` | run-time | new published estimate → flag if outlier vs history/cross-method | triggers auto-retract to review |
| `category-scaffolder` | build-time | category spec → new `categories/<x>/` module + tests | runs under Hermes/Claude Code |
| `test-author` | build-time | methodology section → pytest with hand-computed expected values | tests-first |

Deterministic checks always outrank judge agents: if AI disagrees with a deterministic calculation, trust the calculation.

### 9. Using Paperclip + Hermes + Claude Code together (and Modal)

**Division of labor:**
- **Claude Code (your existing Max 5x, $100/mo)** = the underlying worker. Both Hermes and Paperclip drive it and reuse its credential store (no API keys passed). On 5x, run **single-agent passes** — avoid parallel agent teams / heavy heartbeats that would blow the weekly rate limit.
- **Hermes Agent** = **build-time** orchestration. Drives Claude Code as "Lead Engineer" against the `.steering/` spec sequence (refactor into the plugin architecture, scaffold categories, write tests).
- **Paperclip** = **run-time "company."** Models WCED as an org chart of agents with roles, per-agent budgets, heartbeats, audit logs, and review queues.
- **Modal** = **where the run-time pipeline and API actually execute** (serverless; funded by the $1,000 credits).

**Hermes config (`~/.hermes/config.yaml`):** `model.default: anthropic/claude-opus-4.7`; `delegation.max_iterations: 50`, `max_concurrent_children: 1` (keep low on Max 5x), `child_timeout_seconds: 600`; `terminal.backend: docker`. Drive Claude Code in print mode:

```
claude -p 'Refactor detect/ into categories/oil_fuel_fire/ behind EmissionCategory' \
  --allowedTools 'Read,Edit' --max-turns 10
```
…and interactive tmux for supervised work. Checkpoints + `/rollback` give safe undo.

**Paperclip config:** `npx paperclipai onboard --yes` (Node ≥20, pnpm ≥9.15). Hire agents with the `claude_local` adapter and a budget cap:

```json
{ "adapterType": "claude_local",
  "adapterConfig": { "model": "claude-sonnet-4-6", "maxTurnsPerRun": 150,
                     "timeoutSec": 1800, "instructionsFilePath": "/srv/wced/AGENTS.md" },
  "budgetMonthlyCents": 0 }
```
Heartbeats: daily ingest `intervalSec: 86400`; verification on `@-mention`/task triggers; weekly literature monitor. Keep heartbeats conservative on Max 5x.

**Modal hosting (the $1,000-credit core):**
```python
import modal
app = modal.App("wced")
image = modal.Image.debian_slim().pip_install("fastapi[standard]", "httpx", "numpy", ...)

@app.function(image=image, schedule=modal.Cron("0 6 * * *"))  # daily ingest
def daily_ingest(): ...

@app.function(image=image)
@modal.asgi_app()  # serves the existing FastAPI app, scales to zero
def api(): from wced.api import app as fastapi_app; return fastapi_app
```
Modal runs the pipeline on a schedule and serves the API serverlessly (scales to zero between requests), so the credits stretch a long way for a once-daily batch workload. The **database is NOT on Modal** — point `DATABASE_URL` at a free Neon/Supabase PostGIS instance (see §12).

### 10. Recommended daily/weekly cadence for the solo founder

**Daily (~60–90 min human):**
- **Morning (30–45 min):** review Paperclip's overnight outputs — newly auto-published `Confirmed`/`Verified` incidents + anything `anomaly-watch` flagged; approve held `Reported`/`Suspected` items. This is your editorial/scientific gate.
- **Midday:** one high-leverage building task via Hermes/Claude Code.
- **Evening (15 min):** check Modal usage + Paperclip budgets, queue overnight Hermes build jobs, update `HANDOFF.md`.

**Weekly:** run `methodology-drift` + `literature-monitor`; review proposed diffs; tag a release + mint a Zenodo DOI if methodology/data changed; run the full `tests/methodology/` suite + a backfill validation. **You are the bottleneck and the scientific authority — protect that time and automate everything else.**

### 11. Ensuring scientific accuracy & reproducibility under model-(b) auto-publish

Auto-publishing is safe only with these enforced **in code**:
1. **Confidence-gating**: only `Confirmed`/`Verified` (≥2 independent sources OR satellite confirmation) auto-publish; the rest go to a hold queue. Encode in the publish function.
2. **Mandatory provenance + uncertainty**: the publish endpoint rejects any estimate lacking a complete `ProvenanceRecord` chain or a `Distribution` with ≥10,000 samples.
3. **Cross-method gate**: bottom-up vs top-down divergence beyond tolerance → route to review.
4. **Anomaly flagging**: `anomaly-watch` auto-retracts outliers to `PENDING_REVIEW` and posts a public "under review" note.
5. **One-command rollback**: `wced verify retract <event_id>` + methodology rollback; every transition appended to `publication_log`.
6. **Versioned methodology + reproducible pipeline**: semver, `methodology_version` on every estimate, recompute only via explicit `wced recompute`, pinned-seed notebooks, Zenodo DOIs.
7. **Eval/harness discipline**: `tests/methodology/` (hand-computed expected values) is the final authority; LLM-as-judge is advisory. Target ≥80% coverage on `quantify/` and `provenance/`.

### 12. Scaling from MVP to production

- **Emission-category sequencing**: V1 = oil/fuel fires (hardening). Then destroyed buildings, combat fuel, munitions embodied carbon, shipping rerouting, reconstruction projections — each a new `categories/<x>/` plugin with its own methodology version and tests.
- **Infrastructure scaling**: stay serverless on Modal as long as the credits last; the workload (daily batch + scale-to-zero API) is a near-perfect fit. Database scales within the free Postgres tier until data outgrows it.
- **Agent scaling**: add Paperclip agents per category (each budget-capped) as categories come online; keep deterministic verification central. Watch multi-agent cost multipliers — another reason to stay single-agent on Max 5x.
- **Cost trajectory**: **MVP = $0/mo additional cash.** Claude Max 5x ($100/mo) is already owned; Modal compute is covered by the $1,000 credits; Postgres + frontend + all data sources are free-tier. For a once-daily batch + scale-to-zero API, the $1,000 Modal credits should provide **many months of runway** — track burn in the Modal dashboard. The first real cash cost only appears when (a) Modal credits run out, or (b) data outgrows the free Postgres tier — at which point a flat ~$50/mo Hetzner VPS or a ~$19/mo Neon Launch plan is the next step, not per-token API bills.

**Hosting recommendation (revised):**
- **Compute & API → Modal.com**, funded by your $1,000 credits. Modal does cron scheduling (`modal.Cron`) and serverless FastAPI (`@modal.asgi_app`) with scale-to-zero, so a daily pipeline + an API that's idle most of the time costs very little against the credit balance. This is the best use of credits you already have.
- **Database → Neon (recommended) or Supabase**, free tier, both support the **PostGIS** extension. **Neon** scales to zero (matches Modal's serverless model) and gives ~190 free compute-hours/month — ample for a daily-triggered pipeline; note the free tier is single-region with ~300–500 ms cold starts (irrelevant for batch). **Supabase** free tier gives 500 MB DB but **pauses after 7 days of inactivity** — your daily Modal cron keeps it awake, so either works. Pick Neon for purity, Supabase if you want bundled auth/REST later.
- **Frontend → Vercel or Cloudflare Pages**, free tier (Next.js deploys free).
- **Agent orchestration → local machine.** Run Claude Code / Hermes / Paperclip on your own machine; they don't need a server, and this keeps them on your Max allocation. Only move Paperclip to a host if you need 24/7 unattended heartbeats — and even then, Modal or the Neon-adjacent free compute can host it before you pay for a VPS.

---

## Recommendations (staged)

**Stage 0 — this week (decision + safety rails + free infra):**
1. Replace the CLAUDE.md "no auto-publish for 6 months" rule with the **confidence-gated auto-publish policy** (§11). Everything depends on this.
2. **Provision free infra:** create a Neon (or Supabase) project, enable PostGIS, copy `DATABASE_URL`; deploy the FastAPI app + daily-ingest cron to **Modal** using the credits; deploy the Next.js frontend to Vercel/Cloudflare Pages. Point the existing `just bootstrap` at the Neon URL.
3. Install Claude Code (Max 5x), Hermes (`pip install hermes-agent`), Paperclip (`npx paperclipai onboard`) locally. Wire all to the Claude credential store. Set `--max-turns` defaults and `budgetMonthlyCents: 0` on every agent.
4. **Remove the ACLED connector**; wire in GDELT as the conflict-event source and UCDP for historical backfill.

**Stage 1 — weeks 2–4 (plugin refactor + auto-publish):** Hermes-driven Claude Code refactors `detect/quantify` into `categories/oil_fuel_fire/` behind `EmissionCategory`; build the publication gate, `anomaly-watch`, the post-hoc review queue, and the public revision log. Tests first.

**Stage 2 — weeks 5–8 (run-time company + research loop):** Stand up the Paperclip org chart for daily ops; add `literature-monitor` + `methodology-drift`; connect Zenodo; lock the daily/weekly cadence.

**Stage 3 — quarter 2 (scale):** Add the second emission category as a plugin; start a draft paper from the reproducible outputs.

**Thresholds that change the plan:** if Claude Max 5x weekly rate limits throttle your build velocity, **throttle to single-agent print-mode passes and batch build work** rather than adding paid API capacity (you've chosen $0 spend). If Modal credits run low, lengthen cron cadence and rely more on scale-to-zero before adding any paid host. If the free Postgres tier fills up, prune raw snapshots (keep provenance IDs, not rasters) before upgrading. If auto-publish ever emits a wrong number that reaches users, **tighten the confidence gate** (e.g., require cross-method agreement) rather than abandoning model-(b).

---

## Caveats

- **Tool-definition discrepancies (unchanged from v1).** "Nightshift" appears to be a separate project (by Orbit), not a Hermes feature; Hermes' real autonomous mechanisms are Docker-isolated `terminal` sessions, a `goal_judge` completion model, and `/rollback` checkpoints. The "0.9 rollback threshold" could not be found in any Hermes primary source. Build on the verified mechanisms.
- **Modal credit runway is finite — track it.** $1,000 is generous for a daily batch + scale-to-zero API, but watch the Modal billing dashboard; GPU work (e.g. heavier vision models) burns credits far faster than CPU pipeline runs. Keep Sentinel/vision processing modest at MVP.
- **Free Postgres limits.** Neon free ≈ 0.5 GB storage + 190 compute-hours/mo; Supabase free = 500 MB + pauses after 7 days idle. WCED's data (events/facilities/estimates — text + numbers, no rasters) fits easily at MVP, but **never store raw satellite rasters in Postgres** — keep only granule IDs in provenance (an existing repo rule that also protects your free tier).
- **Paperclip $0 cost display is a known artifact**, not real savings tracking. Track Claude Max usage and Modal credits separately so you don't fly blind.
- **Claude Max + third-party tools.** Verify Hermes/Paperclip draw on your Max 5x allocation rather than silently switching to metered billing — there have been reports of token inflation burning Max limits faster than expected. Since you've chosen $0 spend, the mitigation is **throttling (single-agent, conservative heartbeats)**, not a paid fallback key.
- **ACLED removal is a real methodology change** — re-version the methodology (e.g., v1.1.0) to record that conflict corroboration now uses GDELT/UCDP instead of ACLED, with a CHANGELOG entry. Don't silently swap sources on a scientific project.
- **Emission-factor versioning.** EPA revises factors periodically; pin the source-year in `data/emission_factors.yaml` and re-version when a factor changes.
- **Latency honesty.** FIRMS has ~3-hour global latency; GDELT/news lag further — keep the "near-real-time, updated daily" framing; never claim live.
- **Attribution & dual-use.** Keep emissions aggregated, store official statements as `Claimed`, run the sub-100m coordinate dual-use checklist — these matter more once publishing is automated.
- **Single-point-of-failure.** A solo, auto-publishing operation has no human redundancy; the confidence gate, anomaly auto-retract, and one-command rollback are what make it acceptable. Don't loosen them to move faster.
