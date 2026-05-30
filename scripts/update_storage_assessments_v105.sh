#!/usr/bin/env bash
# v1.0.5: Delete and re-insert damage assessments for 10 storage-type events
# with tightened fraction-destroyed defaults (0.05, 0.15, 0.30).
#
# Previous defaults:
#   OIL_DEPOT / STORAGE_TANK_FARM: 0.20, 0.40, 0.65
#   REFINERY:                      0.10, 0.20, 0.35
#
# New defaults (all storage types):
#   0.05, 0.15, 0.30
#
# See methodology/CHANGELOG.md v1.0.5 for rationale.
set -euo pipefail

# --- Step 1: Delete existing damage assessments for the 10 storage-type events ---

STORAGE_EVENT_IDS=(
  # OIL_DEPOT — Ahvaz / Karoon (5 events)
  "b6bb4774-b19b-450e-a9d7-0607fdade626"
  "4c47efd0-ad5d-4a35-a8ea-5ad19628e686"
  "e798aafd-95e4-48cc-b63a-dd0bd471fcc3"
  "6dc106ea-6dcd-49c2-ba1b-a84a9d8214ea"
  "b2e25d57-398e-4fb0-b2d7-069e89b2f241"
  # OIL_DEPOT — Aghajari (3 events)
  "c5286ae9-d607-4120-b447-e846408ade64"
  "c067d3ed-7174-47d9-9579-0fe154e06cc0"
  "18bcc2db-3849-42b2-a0fd-d24093b68d32"
  # STORAGE_TANK_FARM — Tehran south tank farm
  "a3ff24e4-03c6-4a7c-85ae-a6d1208915f8"
  # REFINERY — Abadan Refinery
  "0a993bde-2887-4f10-a57e-7a21bdb1bad4"
)

echo "Deleting existing damage assessments for ${#STORAGE_EVENT_IDS[@]} storage-type events..."

for eid in "${STORAGE_EVENT_IDS[@]}"; do
  psql "${WCED_DB_DSN}" -c "DELETE FROM damage_assessments WHERE event_id = '${eid}';"
done

echo "Deleted. Re-inserting with fraction-destroyed (0.05, 0.15, 0.30)..."

# --- Step 2: Re-insert with new fraction-destroyed defaults ---

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=19875.44 tCO2e
wced verify add-assessment b6bb4774-b19b-450e-a9d7-0607fdade626 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.20,0.40,0.65) yielded rho>2.0"

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=16078.62 tCO2e
wced verify add-assessment 4c47efd0-ad5d-4a35-a8ea-5ad19628e686 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.20,0.40,0.65) yielded rho>2.0"

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=2027.14 tCO2e
wced verify add-assessment e798aafd-95e4-48cc-b63a-dd0bd471fcc3 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.20,0.40,0.65) yielded rho>2.0"

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=1886.36 tCO2e
wced verify add-assessment 6dc106ea-6dcd-49c2-ba1b-a84a9d8214ea \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.20,0.40,0.65) yielded rho>2.0"

# OIL_DEPOT — Ahvaz / Karoon production area — FRP p50=206.30 tCO2e
wced verify add-assessment b2e25d57-398e-4fb0-b2d7-069e89b2f241 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.20,0.40,0.65) yielded rho>2.0"

# OIL_DEPOT — Aghajari oil field — FRP p50=1153.88 tCO2e
wced verify add-assessment c5286ae9-d607-4120-b447-e846408ade64 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.20,0.40,0.65) yielded rho>2.0"

# OIL_DEPOT — Aghajari oil field — FRP p50=628.22 tCO2e
wced verify add-assessment c067d3ed-7174-47d9-9579-0fe154e06cc0 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.20,0.40,0.65) yielded rho>2.0"

# OIL_DEPOT — Aghajari oil field — FRP p50=570.40 tCO2e
wced verify add-assessment 18bcc2db-3849-42b2-a0fd-d24093b68d32 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.20,0.40,0.65) yielded rho>2.0"

# STORAGE_TANK_FARM — Tehran Refinery (south tank farm) — FRP p50=3310.69 tCO2e
wced verify add-assessment a3ff24e4-03c6-4a7c-85ae-a6d1208915f8 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.20,0.40,0.65) yielded rho>2.0"

# REFINERY — Abadan Refinery — FRP p50=239.88 tCO2e
wced verify add-assessment 0a993bde-2887-4f10-a57e-7a21bdb1bad4 \
  --reviewer "system_review" \
  --fraction-destroyed 0.05,0.15,0.30 \
  --assessment-method EXPERT_ESTIMATE \
  --notes "v1.0.5 FRP-informed fraction-destroyed; previous (0.10,0.20,0.35) yielded rho>2.0"

echo ""
echo "Done. ${#STORAGE_EVENT_IDS[@]} damage assessments updated to (0.05, 0.15, 0.30)."
echo "Next: run 'wced recompute --methodology-version 1.0.5 --yes'"
