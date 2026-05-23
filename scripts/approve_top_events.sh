#!/usr/bin/env bash
# Attach damage assessments to the top 27 PUBLISHED events (v1.0.2 p50 > 100 tCO2e).
# Uses 'wced verify add-assessment' (not approve) because events are already PUBLISHED.
#
# Facility-type fraction-destroyed defaults (Triangular low,mode,high):
#   REFINERY:                      0.05, 0.15, 0.30  (v1.0.5; was 0.10,0.20,0.35)
#   OIL_DEPOT / STORAGE_TANK_FARM: 0.05, 0.15, 0.30  (v1.0.5; was 0.20,0.40,0.65)
#   PETROCHEMICAL:                 0.10, 0.25, 0.45
#   GAS_PROCESSING:                0.05, 0.15, 0.30
#
# Generated: 2026-05-24 from methodology_version=1.0.2, method=FRP, p50>100
set -euo pipefail

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=19875.44 tCO2e
wced verify add-assessment b6bb4774-b19b-450e-a9d7-0607fdade626 \
  --reviewer "system_review" \
  --fraction-destroyed 0.20,0.40,0.65 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=16078.62 tCO2e
wced verify add-assessment 4c47efd0-ad5d-4a35-a8ea-5ad19628e686 \
  --reviewer "system_review" \
  --fraction-destroyed 0.20,0.40,0.65 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — South Pars / Asaluyeh gas complex — FRP p50=7727.82 tCO2e
wced verify add-assessment 354a1cfe-86b3-4599-a776-e94229a88fb5 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# STORAGE_TANK_FARM — Tehran Refinery (south tank farm) — FRP p50=3310.69 tCO2e
wced verify add-assessment a3ff24e4-03c6-4a7c-85ae-a6d1208915f8 \
  --reviewer "system_review" \
  --fraction-destroyed 0.20,0.40,0.65 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — Ras Laffan Industrial City — FRP p50=3174.07 tCO2e
wced verify add-assessment 0b49798e-052f-49d8-8836-32b77407597d \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — South Pars / Asaluyeh gas complex — FRP p50=2337.14 tCO2e
wced verify add-assessment 06ca5ef5-8546-48a6-89b6-1b6cbbe9c662 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=2027.14 tCO2e
wced verify add-assessment e798aafd-95e4-48cc-b63a-dd0bd471fcc3 \
  --reviewer "system_review" \
  --fraction-destroyed 0.20,0.40,0.65 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — South Pars / Asaluyeh gas complex — FRP p50=2019.15 tCO2e
wced verify add-assessment 774b3185-7ea2-49c3-8ac2-8d386b2222c0 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=1886.36 tCO2e
wced verify add-assessment 6dc106ea-6dcd-49c2-ba1b-a84a9d8214ea \
  --reviewer "system_review" \
  --fraction-destroyed 0.20,0.40,0.65 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# PETROCHEMICAL — Bandar Imam Khomeini Petrochemical — FRP p50=1314.39 tCO2e
wced verify add-assessment 1407ef17-fddf-419b-9458-f2ddf4d7b268 \
  --reviewer "system_review" \
  --fraction-destroyed 0.10,0.25,0.45 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — Ras Laffan Industrial City — FRP p50=1182.46 tCO2e
wced verify add-assessment c925fabb-bbec-4880-acc3-4cc11a30cf14 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# OIL_DEPOT — Aghajari oil field — FRP p50=1153.88 tCO2e
wced verify add-assessment c5286ae9-d607-4120-b447-e846408ade64 \
  --reviewer "system_review" \
  --fraction-destroyed 0.20,0.40,0.65 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# PETROCHEMICAL — Bandar Imam Khomeini Petrochemical — FRP p50=950.88 tCO2e
wced verify add-assessment 6ca2d245-7f8a-4a47-b78b-0c39ea991af9 \
  --reviewer "system_review" \
  --fraction-destroyed 0.10,0.25,0.45 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — South Pars / Asaluyeh gas complex — FRP p50=841.41 tCO2e
wced verify add-assessment dbec2b14-ae68-450f-825f-d38627f8878b \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — Ras Laffan Industrial City — FRP p50=806.77 tCO2e
wced verify add-assessment 2661e12a-6162-4edb-9630-6dafbedcfe91 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# OIL_DEPOT — Aghajari oil field — FRP p50=628.22 tCO2e
wced verify add-assessment c067d3ed-7174-47d9-9579-0fe154e06cc0 \
  --reviewer "system_review" \
  --fraction-destroyed 0.20,0.40,0.65 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# OIL_DEPOT — Aghajari oil field — FRP p50=570.40 tCO2e
wced verify add-assessment 18bcc2db-3849-42b2-a0fd-d24093b68d32 \
  --reviewer "system_review" \
  --fraction-destroyed 0.20,0.40,0.65 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — Ras Laffan Industrial City — FRP p50=556.56 tCO2e
wced verify add-assessment 1e108aeb-d3da-43fc-a929-48918ae3d633 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — Ras Laffan Industrial City — FRP p50=554.46 tCO2e
wced verify add-assessment a207f55f-87f0-4d3e-870d-ea055407da3b \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — South Pars / Asaluyeh gas complex — FRP p50=353.06 tCO2e
wced verify add-assessment 955a1d9d-4dfd-4bf9-b762-bbb25208f42b \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — Ras Laffan Industrial City — FRP p50=349.79 tCO2e
wced verify add-assessment 381d8732-ff8f-4f97-a65e-4d4eb065a98c \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# GAS_PROCESSING — South Pars / Asaluyeh gas complex — FRP p50=301.04 tCO2e
wced verify add-assessment a193a3cf-7942-405a-a0d0-479027372271 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# REFINERY — Abadan Refinery — FRP p50=239.88 tCO2e
wced verify add-assessment 0a993bde-2887-4f10-a57e-7a21bdb1bad4 \
  --reviewer "system_review" \
  --fraction-destroyed 0.10,0.20,0.35 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=206.30 tCO2e
wced verify add-assessment b2e25d57-398e-4fb0-b2d7-069e89b2f241 \
  --reviewer "system_review" \
  --fraction-destroyed 0.20,0.40,0.65 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# PETROCHEMICAL — Bandar Imam Khomeini Petrochemical — FRP p50=118.19 tCO2e
wced verify add-assessment f5a91ed8-34d2-476a-9105-c00e9082ed0f \
  --reviewer "system_review" \
  --fraction-destroyed 0.10,0.25,0.45 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# PETROCHEMICAL — Bandar Imam Khomeini Petrochemical — FRP p50=116.22 tCO2e
wced verify add-assessment 0dc808be-e3a9-486b-a9bb-2bcb4d7c069a \
  --reviewer "system_review" \
  --fraction-destroyed 0.10,0.25,0.45 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"

# PETROCHEMICAL — Bandar Imam Khomeini Petrochemical — FRP p50=114.05 tCO2e
wced verify add-assessment 1f19671e-b647-4398-9da8-f873d6f3863c \
  --reviewer "system_review" \
  --fraction-destroyed 0.10,0.25,0.45 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.2 baseline-subtracted; pre-war p75 baseline applied"
