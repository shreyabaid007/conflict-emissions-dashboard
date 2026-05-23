# product.md — Product Vision

## What

A public, near-real-time dashboard that quantifies CO₂ emissions from oil and fuel infrastructure fires caused by the 2026 Iran–US–Israel war, with full source provenance, explicit uncertainty bounds, and version-controlled methodology.

## Why

Military and conflict emissions are the largest blind spot in global climate accounting (~5.5% of global GHGs, ≥82% unreported). The 2026 Iran war is producing measurable carbon pollution from oil/fuel fires every day, but no existing tool tracks it in near-real-time with academic rigor. PDFs published weeks after the fact don't move policy. A continuously updated, publicly auditable dashboard does.

## Who (User Personas)

### Primary: Climate-conflict researchers
- Need: structured data and methodology to cite in papers
- Pain: existing estimates are static PDFs without access to underlying data
- Win condition: can download per-event CSV with full provenance and replicate any number

### Secondary: Climate journalists
- Need: trustworthy headline numbers with confidence indicators
- Pain: existing numbers feel made up; can't explain methodology to editors
- Win condition: every number has a sourced explanation they can quote

### Tertiary: Policy analysts (UNFCCC, think tanks)
- Need: aggregated estimates with defensible uncertainty bounds
- Pain: cannot bring NGO point estimates to negotiations without uncertainty
- Win condition: dashboard provides 5/50/95 percentile bounds suitable for IPCC-style framing

### Quaternary: Iranian civil society and diaspora
- Need: documentation of environmental damage independent of state narratives
- Pain: state media on all sides distorts data
- Win condition: methodology is transparent and apolitical enough to be trusted by all

## What This Product Is NOT

- Not a news ticker — events are vetted before publication, not pushed live
- Not an alert system — no warnings, no predictions, only post-event reporting
- Not a casualty tracker — out of scope, see ACLED
- Not a damage assessment platform — see Conflict Ecology Lab portal
- Not a tool for legal accountability — uncertainty bounds too wide for evidentiary use
- Not partisan — does not advocate for or against the war

## Success Metrics (in order of priority)

1. **Citation in ≥3 peer-reviewed papers within 18 months** of launch
2. **Methodology adopted by ≥1 academic project for a different conflict** (Ukraine, Sudan, Gaza)
3. **Zero retractions due to methodology error** (revisions due to better data are fine and expected)
4. **Public methodology PDF reviewed by named external auditors** (e.g., SEI, ETH Zurich)
5. **API used by ≥1 reputable media outlet** (FT, Guardian, WaPo)

Vanity metrics we explicitly ignore: page views, social engagement, mainstream news mentions.

## Design Principles

- **Boring numbers beat dramatic numbers.** Conservative estimates with wide bounds are more useful than precise-sounding point estimates.
- **The methodology is the product.** The dashboard is the methodology made visible.
- **Audit trails over UX polish.** A clunky page with bulletproof provenance is better than a beautiful page with hand-wavy sources.
- **Stop publishing rather than publish wrong.** If verification fails, the incident waits.

## Roadmap Bands (Capability, Not Calendar)

**V1 — Foundation:** Oil/fuel fire emissions only, near-real-time daily updates, full provenance, Monte Carlo uncertainty.

**V2 — Validation maturity:** TROPOMI top-down validation operational; cross-validation between FRP and inventory methods automated; external methodology audit completed.

**V3 — Multi-category expansion:** Building destruction emissions (partnering with Conflict Ecology Lab CCD layer); equipment + munitions embodied carbon module.

**V4 — Second-order emissions:** Combat aircraft fuel (parametric); Hormuz shipping rerouting impact.

**V5 — Ecological cost intelligence platform:** Beyond carbon — oil-spill volume estimates, protected-area biodiversity impact flags, water/soil contamination indicators. The platform becomes a multi-conflict observatory.

## Constraints and Trade-offs We Accept

- We will be slower than news media. That's intentional.
- We will sometimes be unable to publish high-profile events because verification fails. That's intentional.
- Our numbers will be revised. That's intentional — we version transparently.
- We will be criticized as either downplaying or exaggerating depending on the critic's politics. Open methodology is our only defense.

## Out of Scope (Forever, Not Just V1)

- Predictions about future strikes
- Real-time tactical information
- Attribution to specific military units
- Casualty estimates
- Property damage in monetary terms
- Legal opinions on ecocide / international law (we cite others, we don't opine)
