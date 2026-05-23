# War Carbon Emissions Dashboard — V1 Plan

## TL;DR: The Best V1 Category

**Target: Oil & fuel infrastructure fire emissions in Iran and the Gulf region (Feb 28, 2026 – present).**

This is the single best starting category because it is the only conflict-emissions line item that satisfies all four criteria simultaneously:

1. **Directly observable** from public satellite data (no parametric guesswork required for detection)
2. **Defensible emission factors** exist with peer-reviewed precedent (Kuwait 1991 oil fires)
3. **High signal-to-noise** — refinery and depot fires are large, hot, sustained thermal anomalies that are hard to fake or confuse with background
4. **Politically narrow** — measures the carbon cost of burning hydrocarbons, sidestepping attribution debates about who fired the missile

It is also the **second-largest line item** in the CCI 14-day Iran estimate (1.88 Mt CO₂e from 2.5–5.9M barrels burned) — meaning a V1 covering only this category captures ~37% of total direct conflict emissions while remaining methodologically airtight.

Other categories considered and deferred:
- **Destroyed buildings** (largest category at 2.4 Mt) — requires SAR coherent-change-detection pipeline (expensive, slow); deferred to V2.
- **Combat aircraft fuel** — depends on sortie counts not in public data; parametric only.
- **Munitions embodied carbon** — requires per-platform LCA data; small contribution.
- **Reconstruction** — projection, not measurement; defer to V3.

---

## Why "Fire Emissions" Is The Right MVP

### Data availability is genuinely real-time

| Data Source | Latency | Resolution | Cost |
|---|---|---|---|
| NASA FIRMS (VIIRS) | ~3 hours | 375 m | Free |
| NASA FIRMS (MODIS) | ~3 hours | 1 km | Free |
| Sentinel-2 (SWIR bands) | 5-day revisit | 20 m | Free |
| Sentinel-3 SLSTR (FRP) | ~daily | 1 km | Free |
| Sentinel-5P TROPOMI (NO₂/CO/SO₂) | ~daily | 5.5 km | Free |
| GOES-16/Meteosat (geostationary) | 10–15 min | 2 km | Free |

You can detect a refinery fire within hours, confirm it within a day, and back-calculate emissions within a week — all from free public data.

### Established peer-reviewed methodology exists

Two complementary approaches:

**(A) Bottom-up: Fuel inventory × combustion factor**
- For each confirmed strike on an oil facility, estimate fuel/feedstock destroyed × emission factor
- Crude oil: 425 kg CO₂/barrel (used by CCI Iran brief, consistent with EPA AP-42)
- Refined products: 0.39 t CO₂/barrel (gasoline) to 0.43 t CO₂/barrel (heavy fuel oil)

**(B) Top-down: Fire Radiative Power (FRP) integration**
- FRP measured by VIIRS / SLSTR / MODIS in megawatts
- Combustion rate (kg/s) = FRP × biome-specific coefficient
- Integrate over fire duration → total fuel mass burned → CO₂ via combustion stoichiometry
- For hydrocarbon fires: ~3.15 kg CO₂ per kg crude burned (assuming ~96% carbon recovery as CO₂, per Hobbs & Radke 1992, *Science* 256:987)

**Cross-validation between (A) and (B) is the dashboard's signature credibility move.**

---

## V1 Methodology Specification

### 1. Detection layer
- Ingest FIRMS daily for Iran + Gulf bounding box
- Spatial filter: only retain hotspots within 500 m of known oil/gas/petrochemical facilities
- Maintain a curated facility geometry registry (OSM + Global Energy Monitor + manually verified)
- Temporal filter: persistent hotspots (≥2 consecutive overpasses) flagged as "fire event"

### 2. Verification layer
- For each fire event, fetch Sentinel-2 / Planet imagery within ±72 hours
- AI-assisted classification: confirmed fire vs. flaring vs. false positive (gas flares are constant; conflict fires are sudden + sustained)
- Cross-reference with ACLED conflict events within 24-hour window at same coordinates
- Confidence label assigned: **Confirmed** (FIRMS + Sentinel-2 + ACLED match) / **Verified** (FIRMS + Sentinel-2) / **Reported** (FIRMS only) / **Suspected** (single overpass)

### 3. Quantification layer (per fire event)

**FRP-based estimate:**
```
fuel_burned_kg = ∫ (FRP_MW × 0.368 kg/MJ × 1000) dt
CO₂_kg = fuel_burned_kg × 3.15
```

**Inventory-based estimate (when facility capacity is known):**
```
fuel_destroyed_barrels = min(facility_capacity, observed_burn_duration × burn_rate)
CO₂_kg = fuel_destroyed_barrels × 425
```

**Reported estimate** (cross-check): If CEOBS/CCI/news report a specific volume → use as third number.

### 4. Uncertainty layer
- Monte Carlo with explicit parameter PDFs:
  - FRP-to-combustion coefficient: Normal(0.368, 0.05) kg/MJ
  - Carbon recovery as CO₂: Triangular(0.92, 0.96, 0.98)
  - Facility capacity uncertainty: ±30% (from Global Energy Monitor metadata)
  - Burn duration: ±20% (from satellite revisit gaps)
- Report 5th / 50th / 95th percentile per event AND aggregate
- Compute discrepancy between FRP-based and inventory-based estimates; flag events where ratio > 2×

### 5. TROPOMI top-down validation (weekly batch)
- For each fire event, query TROPOMI NO₂ and CO for downwind plumes
- Run HYSPLIT/FLEXPART back-trajectory
- Compare integrated plume CO₂ proxy to bottom-up estimate
- Discrepancy > 2× → trigger methodology review (logged publicly)

---

## What the V1 Dashboard Displays

### Headline (top of page)
- **Running total CO₂ from oil/fuel fires** (with 5–95% bounds prominently)
- Days since conflict start: 28 Feb 2026
- Number of confirmed fire events
- "Equivalent to X days of Iran's pre-war emissions" (Carbon Monitor reference)

### Map view
- Iran + Gulf bounding box
- Pins per fire event, color-coded by confidence label
- Sized by estimated emissions
- Click → event detail panel

### Event detail panel
- Date, location, target name
- FIRMS timeline (FRP over time chart)
- Sentinel-2 before/after imagery
- ACLED corroboration if any
- Three emission estimates (FRP-based, inventory-based, reported) with bounds
- Source provenance chain

### Time series
- Daily cumulative CO₂ with uncertainty band
- Stacked area by target type (refinery / depot / petrochemical / tanker / offshore)
- Spike annotations linked to major incidents

### Methodology page
- Full equations, all factors with citations
- Open changelog of every methodology revision
- Link to GitHub repository

### Data download
- CC-BY 4.0 licensed CSV, JSON, GeoJSON
- Per-event records and aggregated daily/weekly series

---

## How V1 Evolves Into V2/V3

| Version | Scope Addition | New Data Needs | Timeline |
|---|---|---|---|
| **V1** | Oil/fuel fire CO₂ | FIRMS + S2 + TROPOMI + ACLED | Month 0–4 |
| **V1.5** | TROPOMI top-down validation matured | HYSPLIT/FLEXPART integration | Month 4–6 |
| **V2** | Building destruction emissions | Sentinel-1 SAR CCD pipeline (Conflict Ecology Lab partnership) | Month 6–10 |
| **V2.5** | Munitions + equipment embodied carbon | Per-platform LCA database | Month 10–12 |
| **V3** | Combat aviation fuel estimates | Sortie-count parametric model + Flightradar24 archives | Month 12–15 |
| **V3.5** | Shipping rerouting (Hormuz) | AIS analytics partnership | Month 15–18 |
| **V4** | Reconstruction projection scenarios | GCCA cement + worldsteel + Iran-specific factors | Month 18–24 |
| **V5** | Non-carbon ecological cost (oil spills, biodiversity) | PAX partnership, Sentinel-2 oil-slick detection | Month 24+ |

The architecture should be designed for this evolution — **modular emission-category plugins**, not a monolith.

---

## AI Infusion Strategy

AI is used at six specific bottleneck points (no general "AI everywhere" approach):

1. **OSINT triage** — Claude/GPT-4 with structured output to extract incident records from multilingual news/social media
2. **Image classification** — fine-tuned vision models to distinguish fire/flaring/false-positive on Sentinel-2 RGB+SWIR composites
3. **Facility identification** — vision models to identify which storage tanks/units in a refinery were hit
4. **Provenance scoring** — LLM cross-source consistency check producing confidence labels
5. **Parameter retrieval** — RAG agent for on-demand emission factor lookup with citation
6. **Methodology audit** — LLM-as-judge comparing dashboard outputs to new academic publications

Every AI output is **paired with provenance** and **passed through Monte Carlo uncertainty** before display.

---

## Hard Constraints (Read These Before Building)

1. **Latency is NOT real-time.** FIRMS = 3 hours. Sentinel revisit = days. Be honest: market as "near-real-time, updated daily."
2. **False positives are constant.** Gas flares at oil facilities look like fires. Persistent baseline subtraction is mandatory.
3. **Cloud cover blocks optical verification.** Be transparent when a fire is FIRMS-only.
4. **Iranian official sources, Israeli official sources, US official sources, and Gulf media all have axes to grind.** Every claim needs ≥2 independent confirmations or one satellite confirmation.
5. **Do not predict — only report post-event.** Avoid dual-use risks.
6. **Version everything.** Every estimate must be revisable with public audit trail.
7. **The dashboard is a visibility tool, not a legal accountability tool.** Avoid framing that invites legal weaponization.
