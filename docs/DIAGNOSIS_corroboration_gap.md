# Diagnosis — Zero corroboration data (every event S2=no, ACLED=no, GDELT=no)

**Date:** 2026-05-30
**Type:** Diagnostic only. Read-only queries against the local DB (`$LOCAL_SA`,
`localhost:5433/wced`). Nothing was mutated. No fix was run.

## Symptom

The v1.1.0 recompute report shows all 67 events with `S2=no`, `ACLED=no`,
`GDELT=no`. Nothing reaches `VERIFIED`/`CONFIRMED`, totals are unchanged, and the
v1.1.0 "GDELT promotion" has nothing to act on.

## Root cause (one sentence)

**The events were created by the batch backfill path (`FIRMS ingest → wced detect →
wced quantify`), which never runs verification — no GDELT corroboration, no
Sentinel-2 SWIR classification, no `assign_confidence` — so the corroboration/S2
evidence that `recompute` reads (confidence-assignment provenance records) was never
produced or persisted, and every event defaults to `REPORTED`.**

---

## 1. Does `daily_ingest` actually call GDELT ingest and the S2 SWIR check?

**In the flow code: yes, all stages are present and wired (nothing stubbed).**
`wced/pipeline/daily_ingest.py` defines 11 tasks; the relevant ones are real calls,
not stubs:

| Step | Task | What it calls |
|---|---|---|
| 4 | `ingest_conflict_events` | `GDELTConnector.query_events_api(...)` (daily_ingest.py:468) — GDELT is queried; ACLED only if `WCED_ENABLE_ACLED` |
| 6 | `fetch_s2_chips_for_candidates` | `Sentinel2Connector` STAC fetch |
| 7 | `classify_fires` | `classify_fire(...)` SWIR + Claude → `VerificationStatus.VERIFIED/REJECTED/...` |
| 8 | `corroborate_with_conflict_events` | `find_corroboration(...)` (source-agnostic) |
| 9 | `assign_confidence_labels` | `wced.verify.confidence.assign_confidence(...)` |

So GDELT and the S2 check are **not** stubbed and were **not** removed when ACLED was
feature-flagged off — the ACLED→GDELT swap kept GDELT as the primary source.

**But this flow is not what produced the data, and it cannot persist verification
evidence to Postgres:**

1. **`daily_ingest` writes only to in-memory stores.** Tasks 7/8/9 use
   `InMemoryProvenanceStore()` and task 10 uses `InMemoryReviewQueue()`
   (daily_ingest.py:795, 953, 1022). The flow never writes events, conflict events,
   corroboration, or confidence provenance to the database. Its results are
   discarded when the process exits.
2. **It never ran against this DB.** `SELECT count(*) FROM pipeline_runs;` → **0**.
3. **The 67 events came from the backfill path instead.** `scripts/backfill_full_range.py`
   runs `FIRMS ingest → wced detect → wced quantify` and calls **none** of
   corroboration / confidence / GDELT / Sentinel-2 (verified by grep — zero matches).
   And `wced detect` itself (`wced/cli/main.py:1220`) only *"Cluster firms_detections
   rows, match to facilities, and persist FireEvent rows"* — it uses
   `InMemoryProvenanceStore`, calls no verify stage, and **hardcodes every event to
   `ConfidenceLabel.REPORTED`** (main.py:~164).

**Conclusion for Q1:** The path that creates live data (`detect`/backfill) never
calls GDELT or the S2 check. The only path that does (`daily_ingest`) is unrun here
and writes to memory, not the DB.

---

## 2. Are there ANY GDELT records or S2 results in the database?

Read-only counts against local:

| Evidence | Query | Result |
|---|---|---|
| GDELT events table | `to_regclass('public.gdelt_events')` / `conflict_events` | **does not exist** (NULL) |
| GDELT sources | `sources` by `source_type` | only `SATELLITE` ×14 — **no GDELT, no ACLED sources** |
| ACLED events | `count(*) FROM acled_events` | **0** |
| confidence-assignment provenance | `count(*) FROM provenance_records WHERE method LIKE 'confidence_assignment%'` | **0** |
| any `has_gdelt` / `has_acled` / `s2_confirms_fire = true` | over `provenance_records.parameters` | **0 / 0 / 0** |
| S2 chips | `count(*) FROM s2_chips` | **10** (linked to 5 events) |
| FIRMS detections | `count(*) FROM firms_detections` | 1,568,662 |

`provenance_records` only ever come from `wced.detect.hotspot` (47),
`wced.detect.baseline` (14), and `wced.quantify.frp/reconcile/inventory`. **There is
no `wced.verify.confidence` provenance at all.**

Two important nuances:

- **GDELT is structurally unpersistable.** `wced/db/models.py` defines an
  `acled_events` table (a leftover from before the swap) but **no `gdelt_events`
  table**. `GDELTConnector` fetches events into memory; `find_corroboration` consumes
  them in-process; the only place a GDELT match would be recorded is inside a
  `confidence_assignment` provenance record's parameters — which are never written.
  So even a correct run leaves no queryable GDELT rows.
- **The 10 S2 chips are orphaned evidence.** `s2_chips` (backfilled by
  `scripts/backfill_s2_chips.py`) stores only *storage references*
  (`product_id, cloud_cover_pct, storage_path, bands`) — there is **no
  classification / "confirms fire" column**. Nothing reads `s2_chips` during
  recompute. The "S2 confirms fire" boolean lives only in confidence-assignment
  provenance params, which don't exist. So even the 5 events with chips get
  `s2_confirms_fire=False`.

**Conclusion for Q2:** No GDELT data exists or can be persisted; no S2 *result*
exists (only raw chip references for 5 events); no confidence-assignment provenance
exists for any of the 67 events.

---

## 3. Is the corroboration/confidence code reading from the right place?

**The reader is reasonable; the writer never runs — plus a latent linkage bug.**

`wced recompute` derives the flags like this (`wced/cli/main.py:688-724`):

```python
prov_rows = session.execute(
    select(provenance_records.c.parameters)
    .where(provenance_records.c.method.like("confidence_assignment%"))
    .where(provenance_records.c.id.in_(
        select(provenance_inputs.c.input_id)
        .where(provenance_inputs.c.provenance_id == r["provenance_id"])
    ))
).all()
# reads params: has_acled, has_gdelt, s2_confirms_fire  → all default False
new_label = recompute_confidence_label(n_overpasses=2, s2_confirms_fire=has_s2_fire,
    has_acled_corroboration=has_acled, has_gdelt_corroboration=has_gdelt, ...)
```

- It looks for `confidence_assignment%` provenance records linked to the event and
  reads `has_acled`/`has_gdelt`/`s2_confirms_fire` from their parameters. Those
  parameters *are* exactly what `wced.verify.confidence.assign_confidence` writes
  (confidence.py:167-190). So the **read contract is correct** — but there are zero
  such records, so every flag is `False`, and `recompute_confidence_label` lands on
  `REPORTED` for persistent events (confidence.py decision table). This matches the
  report exactly.

- **Latent wiring bug (would bite even after the data path is fixed):** the lookup
  requires the confidence record to be an *input to* the event's provenance record
  (`provenance_inputs.provenance_id == event.provenance_id AND input_id ==
  confidence_record`). But `assign_confidence` builds the confidence record with the
  *candidate's* provenance as **its** input (confidence.py:169 `inputs=[candidate.provenance_id]`)
  — the opposite direction. As written, a persisted confidence record would still not
  be discovered by the recompute query. The link direction must be reconciled.

- The flow’s confidence is also computed against `n_overpasses=2` hardcoded
  (main.py:719), so persistence is assumed, not read from the event.

**Conclusion for Q3:** Not a read-location problem in spirit — recompute reads the
right field from the right table. The gap is that nothing ever *writes* that record
for these events (the verify stage is skipped), and a provenance-link-direction
mismatch would block it even if it did.

---

## Root cause summary

1. Live data is produced by `backfill → wced detect → wced quantify`. `wced detect`
   stamps every event `REPORTED` and runs no verification.
2. GDELT corroboration and S2 classification are therefore never computed for these
   events, and never persisted (no `gdelt_events` table; `s2_chips` holds no result;
   no `confidence_assignment` provenance).
3. `recompute` faithfully reads the (absent) confidence-assignment provenance →
   `has_gdelt = has_acled = s2_confirms_fire = False` for all 67 → `REPORTED`.
4. The only pipeline that performs verification (`daily_ingest`) writes to in-memory
   stores, has never run against this DB, and has no GDELT table to write to anyway.

The v1.1.0 "GDELT promotion" is correct code with **no input data** — not a
methodology error.

---

## Fix (DO NOT RUN — for review)

The verification evidence must be *produced and persisted to the DB* for the existing
events, in the shape `recompute` reads. Recommended sequence:

1. **Add a DB-backed verification/backfill step** that, for each existing
   `fire_event`:
   - queries GDELT over the event's ±window/area (`GDELTConnector.query_events_api`)
     and runs `find_corroboration` → `has_gdelt`;
   - resolves the event's S2 chip (`s2_chips`) and runs `classify_fire` (or persists
     the existing classification) → `s2_confirms_fire`;
   - calls `assign_confidence` with a **DB-backed `ProvenanceStore`** (not
     `InMemoryProvenanceStore`) so the `confidence_assignment` provenance record —
     carrying `has_gdelt`/`has_acled`/`s2_confirms_fire` — is written and linked to
     the event.
   Then re-run `wced recompute --methodology-version 1.1.0`.

2. **Close the structural gaps exposed here** (provenance principle: every number
   must trace to cited sources — GDELT corroboration is currently un-auditable):
   - add a `gdelt_events` / generic `conflict_events` table + repository so GDELT
     corroboration is persistable and auditable;
   - add a classification/result field (or a verification table) so S2 "confirms
     fire" is stored, not just chip references;
   - make `daily_ingest` persist to Postgres (replace the `InMemory*` stores/queue
     with DB-backed repositories) so the live pipeline actually records verification
     evidence and `pipeline_runs`;
   - reconcile the provenance-link **direction** between `assign_confidence`
     (confidence.py:169) and the recompute lookup (main.py:697-704) so a persisted
     confidence record is discoverable from the event.

3. **Reality check before investing:** confirm GDELT actually returns matches for the
   conflict window/AOI (the DOC API only covers the last ~3 months — for a war
   starting 2026-02-28, historical GDELT may need the Events 2.0 flat-file path
   `fetch_latest_events`/BigQuery, not just `query_events_api`). If GDELT genuinely
   has no coverage for these dates, the headline will stay `REPORTED` even after the
   wiring is fixed — that is an honest data limitation, not a bug.

*All findings from read-only SELECTs and source review; no schema or data modified.*
