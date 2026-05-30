# Methodology Changelog

## v1.0.5 — 2026-05-24

**Type:** Parameter calibration (fraction-destroyed defaults for storage-type facilities)

### FRP-Informed Fraction-Destroyed Defaults (§3.4)

**Problem:** The default fraction-destroyed triangular PDFs for storage-type
facilities (OIL_DEPOT, STORAGE_TANK_FARM, REFINERY) were set too high relative
to what FRP observations imply. The inventory method consistently exceeded
the FRP method by 2.7–7.4× for all 10 storage-type events, triggering
`needs_review=True` on every one. The reconciliation ratio ρ > 2.0 for all
cases indicates the prior defaults (OIL_DEPOT/STORAGE_TANK_FARM: 0.20/0.40/0.65;
REFINERY: 0.10/0.20/0.35) systematically overestimated destruction extent.

**Changes:**

1. **Tightened fraction-destroyed defaults for all storage-type facilities**
   to (low=0.05, mode=0.15, high=0.30). This applies to OIL_DEPOT,
   STORAGE_TANK_FARM, and REFINERY facility types.

2. **Deleted and re-inserted all 10 affected damage assessments** with the
   new fraction-destroyed PDF. Assessment method remains EXPERT_ESTIMATE;
   reviewer remains system_review.

3. **No change to inventory equations or reconciliation logic.** The fix is
   purely a parameter recalibration informed by the FRP/inventory ratio
   observed under v1.0.2–v1.0.4.

**Rationale:** Satellite-observed FRP provides an independent constraint on
the total energy released. When inventory estimates systematically exceed
FRP by large multiples, the most likely explanation is that the assumed
fraction destroyed is too high — either the strike damaged a smaller
portion of the facility than assumed, or the facility was not at full
inventory. The new defaults (mode=0.15) reflect that most strikes damage
a minority of storage capacity, consistent with post-strike Sentinel-2
imagery showing localized tank fires rather than facility-wide destruction.

**Expected impact:** Storage-type inventory estimates will decrease by
~2.5–3× on average. Most or all of the 10 storage-type events should
move from `needs_review=True` to `reconciled_ok=True` as the inventory/FRP
ratio falls within the [0.5, 2.0] agreement band.

**Affected outputs:** All `EmissionEstimate` rows for events at OIL_DEPOT,
STORAGE_TANK_FARM, and REFINERY facilities with damage assessments.
Recompute required: `wced recompute --methodology-version 1.0.5`.

## v1.0.4 — 2026-05-24

**Type:** Internal recompute iteration (no code or parameter change)

Recompute of all estimates under v1.0.2 parameters after ingesting additional
FIRMS archival data for the full pre-war window. No equation, parameter, or
code changes; version bump used to tag the recompute run and distinguish its
outputs from the v1.0.3 run. Observations from v1.0.3 and v1.0.4 runs
informed the fraction-destroyed recalibration in v1.0.5.

## v1.0.3 — 2026-05-24

**Type:** Internal recompute iteration (no code or parameter change)

First recompute after v1.0.2 baseline data fix. Used to observe the
FRP-to-inventory ratio across all storage-type facilities with the corrected
pre-war baselines in place. Ratios ranged from 2.7× to 7.4× for all 10
storage-type events, confirming that fraction-destroyed defaults were
systematically too high. No equation, parameter, or code changes.

## v1.0.2 — 2026-05-24

**Type:** Baseline data fix (no structural change to equations or code)

### Pre-War Baseline Sourcing (§3.3)

**Problem:** The FIRMS detection archive in the database only contained
post-war observations (from 2026-02-28 onward). The 30-day rolling
baseline window for events near the war start date found zero qualifying
observations, triggering the fallback baseline (0 MW mean, 50 MW std).
This meant **all routine industrial flaring was attributed to the war**
for facilities with continuous pre-existing flaring (refineries, gas
processing plants, oil production fields).

Ahvaz/Karoon, Aghajari, South Pars, and Bandar Imam Khomeini — the
highest-emitting facilities — all had `insufficient_baseline_history`
flags and zero baseline subtraction in v1.0.1.

**Changes:**

1. **Ingested 12 months of pre-war FIRMS archival data** (2025-02-28 to
   2026-02-27) using NASA FIRMS standard-processing (SP) sources
   (VIIRS_SNPP_SP, VIIRS_NOAA20_SP, MODIS_SP). This provides empirical
   pre-conflict flaring baselines for all registered facilities.

2. **Added `wced ingest firms-historical` CLI command** for backfilling
   archival FIRMS data over arbitrary date ranges. Uses SP sources with
   5-day API chunks (vs 10-day for NRT).

3. **No change to `compute_baseline()` logic.** The rolling 30-day p75
   window and IQR/1.349 robust std from v1.0.1 are unchanged. The fix
   is purely that the data now exists for the window to use.

**Expected impact:** Facilities with continuous pre-war flaring will
show dramatically reduced v1.0.2 estimates. Events whose FRP is fully
explained by the pre-war baseline will be zeroed out. The
`insufficient_baseline_history` flag count should drop to near zero.

**Affected outputs:** All `EmissionEstimate` rows with method=FRP.
Recompute required: `wced recompute --methodology-version 1.0.2`.

## v1.0.1 — 2026-05-24

**Type:** Calibration fix (no structural change to equations)

### Background FRP Baseline Subtraction (§3.3)

**Problem:** Raw FRP was used as event FRP without subtracting routine
industrial flaring. Active refineries with continuous gas flaring had their
emissions overstated by an estimated 20-40%.

**Changes:**

1. **Baseline statistic changed from median to 75th percentile.**
   Flaring is intermittent; the median underestimates characteristic
   background FRP for facilities with episodic flaring patterns.
   The 75th percentile captures the upper end of routine operations.

2. **Baseline uncertainty uses IQR/1.349 (robust std estimator).**
   Replaces population standard deviation (`pstdev`). The IQR-based
   estimator is resistant to outliers from transient flare-ups that
   would inflate the classical std and under-subtract baseline.

3. **Net FRP computed before Monte Carlo:**
   `net_frp_integral = max(0, raw_frp_integral - baseline_frp_mj_per_day * duration_days)`

4. **Insufficient-history flag:** Events within 30 days of war start
   (2026-02-28) use a fallback baseline (0 MW mean, 50 MW std) and are
   tagged with provenance note `insufficient_baseline_history`.

**Affected outputs:** All `EmissionEstimate` rows with method=FRP.
Recompute required: `wced recompute --methodology-version 1.0.1`.

**References:**
- Elvidge et al. 2016 — "Methods for Global Survey of Natural Gas Flaring"
- Freeborn et al. 2014 — FRP–fuel mass relationships

## v1.0 — 2026-03-01

Initial methodology release. See `methodology/v1.0.pdf`.
