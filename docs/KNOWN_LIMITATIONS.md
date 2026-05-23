# Known Limitations (V1)

This document summarizes the verification-layer gap and GDELT-coverage findings
from the V1 development cycle. For the full diagnostic details, see the
original investigation files:

- [`DIAGNOSIS_corroboration_gap.md`](DIAGNOSIS_corroboration_gap.md)
- [`DIAGNOSIS_numbers_unchanged.md`](DIAGNOSIS_numbers_unchanged.md)
- [`GDELT_coverage_check.md`](GDELT_coverage_check.md)

---

## 1. All events are labelled REPORTED

**Root cause:** The detection path that creates live data (`wced detect` /
`scripts/backfill_full_range.py`) runs FIRMS ingest, hotspot clustering,
facility matching, and quantification — but never calls the verification
stages. Every event is hardcoded to `ConfidenceLabel.REPORTED`.

The `daily_ingest` pipeline flow does wire up GDELT corroboration, Sentinel-2
SWIR classification, and confidence assignment — but it writes to in-memory
stores (`InMemoryProvenanceStore`, `InMemoryReviewQueue`), not the database.
It has never been run against the production data. Results are discarded when
the process exits.

**Impact:** The v1.1.0 source-agnostic confidence table is correct code with
no input data. `recompute` faithfully reads the (absent) confidence-assignment
provenance, finds `has_gdelt = has_acled = s2_confirms_fire = False` for all
events, and assigns `REPORTED`. This is not a methodology error — it is a
pipeline-wiring gap.

## 2. GDELT corroboration is structurally impossible via the current connector

The GDELT DOC 2.0 API (used by `wced/ingest/gdelt.py::query_events_api`)
returns article-level metadata with no latitude/longitude fields. The connector
looks for `sourcecountylat`/`sourcecountylon`, which do not exist in the DOC
ArtList response. Every article parses to `lat=lon=0` and is discarded.

GDELT **does** have news coverage for the conflict window (50+ articles found
for broad queries spanning 2026-03 through 2026-05), but the spatial matching
required by `find_corroboration` cannot work without geocoded coordinates.

**Viable alternatives (not yet implemented):**
1. GDELT GEO 2.0 API — returns geocoded points for a query+timespan
2. Events 2.0 historical flat-files — the existing `_parse_csv_row` already
   reads `ActionGeo_Lat/Long`; needs a date-ranged fetch instead of
   `lastupdate.txt`-only
3. GDELT BigQuery — full history with coordinates; heaviest option

Additional issues: aggressive rate limiting (1 req/5s, 429 not retried by the
connector), and narrow per-event date windows returning inconsistent or empty
results.

## 3. Sentinel-2 chips exist but have no classification result

The `s2_chips` table stores 10 chip references (linked to 5 events) with
`product_id`, `cloud_cover_pct`, `storage_path`, and `bands` — but no
classification or "confirms fire" column. The AI classifier
(`wced/ai/classify.py`) exists and works, but its output is never persisted to
the database. The "S2 confirms fire" boolean lives only in
confidence-assignment provenance parameters, which are never written.

## 4. Provenance link direction mismatch

`assign_confidence` (in `wced/verify/confidence.py`) records the confidence
provenance with the candidate's provenance as its *input*. But `wced recompute`
looks for confidence records that are *inputs to* the event's provenance record
— the opposite direction. Even if verification evidence were persisted, the
recompute query would not discover it without reconciling this link direction.

## 5. No `gdelt_events` table

The database schema defines an `acled_events` table (leftover from before the
ACLED-to-GDELT swap) but no `gdelt_events` or generic `conflict_events` table.
GDELT matches exist only in-process during `daily_ingest` and cannot be
persisted or audited.

---

## What this means for headline numbers

The ~75.6 kt CO2e headline is based on FRP and inventory emission estimates
with full Monte Carlo uncertainty bounds (p5/p50/p95). These estimates are
methodologically sound — the gap is in the verification/confidence layer, not
the quantification. Events should be understood as `REPORTED` (detected via
satellite, matched to known facilities) rather than `VERIFIED` or `CONFIRMED`
(independently corroborated).
