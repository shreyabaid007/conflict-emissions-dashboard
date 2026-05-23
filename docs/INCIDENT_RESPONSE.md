# WCED Incident Response Runbook

**Version:** 1.0
**Last updated:** 2026-05-23

**Cardinal rule: never silently delete.** Every correction, retraction, or
revision is a public changelog entry. Retractions are loud, not quiet. The
`editorial_actions` table is append-only — the prior PUBLISHED row remains
in the log so the full audit trail is always visible.

All incident responses use the editorial state machine defined in
`wced/models/editorial.py`. Events transition through:
`PENDING_REVIEW → PUBLISHED → RETRACTED`. There is no path that bypasses
the audit log.

---

## Roles

| Role | Responsibility |
|------|----------------|
| **Incident Lead** | Coordinates response, owns the timeline, writes the postmortem |
| **Editorial Reviewer** | Executes event status transitions via the review queue |
| **Technical Lead** | Investigates root cause in code, data, or pipeline |
| **Communications Lead** | Drafts public statements, manages press and academic contacts |
| **SSC Liaison** | Escalates to the Scientific Steering Committee when methodology is affected |

---

## Scenario 1: Wrong Event Published (Factually False)

An event on the dashboard describes something that did not happen — wrong
facility, wrong date, or no strike occurred at all.

### Detection signals
- External report contradicting the event (media, academic, government source)
- Internal QA review discovers conflicting satellite imagery
- ACLED or CEOBS data contradicts the event after ingestion lag clears
- Community/user report via the feedback channel

### Immediate actions (within 2 hours of confirmation)
1. Incident Lead confirms the event is factually wrong with at least one
   independent source.
2. Editorial Reviewer calls `ReviewQueue.retract(event_id, reviewer=...,
   reason=...)` with a detailed reason string. This transitions the event
   to `RETRACTED` and creates an append-only `EditorialAction` record.
3. The retraction reason becomes a public changelog entry on `/changelog`
   (type: `event_retraction`).
4. Technical Lead preserves all pipeline artifacts (FIRMS detections, S2
   chips, ACLED matches, provenance records) for the postmortem. Nothing
   is deleted.
5. If the event contributed to aggregate totals, Technical Lead triggers a
   recomputation of affected aggregates excluding the retracted event.

### Communication plan
- Within 4 hours: publish a changelog entry (automatic from the retraction).
- Within 24 hours: post a note on the project's public communication channels
  explaining what was wrong, how it passed review, and what is being done.
- If the event was cited by media or researchers: direct outreach to those
  parties with a link to the changelog entry.

### Postmortem template
See [Postmortem Template](#postmortem-template) at the end of this document.

---

## Scenario 2: Emission Estimate Wrong by >2x After New Data

The event is real, but new data (better FRP integral, revised facility
capacity, updated damage assessment) changes the emission estimate by more
than a factor of 2 in either direction.

### Detection signals
- TROPOMI top-down cross-check (`validation_reports.discrepancy_ratio`) flags
  the estimate as an outlier
- Updated FIRMS data (reprocessed collection) changes the FRP integral
- Facility capacity revised after new source material
- Monte Carlo re-run with updated `parameter_distributions.yaml` shifts the
  p50 by >2x
- Manual review during routine QA

### Immediate actions (within 24 hours)
1. Technical Lead documents the old estimate (p5/p50/p95) and the new input
   data that triggered the revision.
2. Technical Lead creates a new `emission_estimates` row with the corrected
   values. The old row is **not deleted** — it remains in the database with
   its original `provenance_id`. The new row gets a new `provenance_id`
   linking to both the original provenance chain and the new data source.
3. Editorial Reviewer records an `editorial_decision` changelog entry with:
   - The event ID
   - Old p50 and new p50 values
   - The reason for the revision and the new data source
4. If the revision crosses a confidence label boundary (e.g., an estimate
   that was CONFIRMED now has insufficient satellite support), re-evaluate
   the confidence label.
5. Recompute affected aggregates and time series.

### Communication plan
- Changelog entry posted immediately (type: `editorial_decision`).
- If the revision changes the project's headline aggregate by >5%, issue a
  dedicated public note.
- If the original estimate was cited externally, notify those parties.

### Postmortem template
See [Postmortem Template](#postmortem-template). Focus on: why the original
estimate was off, whether the pipeline should have caught this earlier, and
what monitoring to add.

---

## Scenario 3: Methodology Error Discovered Post-Launch

An equation in `methodology/v1.0.pdf` is wrong, or its implementation in
`wced/quantify/` diverges from the PDF in a way that produces incorrect
results.

### Detection signals
- External peer review identifies an error in the methodology PDF
- Internal code review discovers implementation diverges from the PDF
- Methodology compliance tests (`tests/methodology/`) fail after a
  previously-passing state
- Academic publication challenges the methodology

### Immediate actions (within 48 hours)
1. SSC Liaison notifies the Scientific Steering Committee immediately.
2. Technical Lead determines the blast radius: which events and estimates
   are affected by the error.
3. If the error **inflates** estimates: add a banner to the dashboard
   stating that affected estimates are under review and may be revised
   downward. Do not retract events — the events are real; only the
   numbers are wrong.
4. If the error **deflates** estimates: same banner, noting revision
   upward.
5. Do **not** recompute estimates until the SSC approves a corrected
   methodology version. All estimates carry a `methodology_version` field;
   recomputation is a deliberate operation, never automatic.

### Communication plan
- Within 24 hours: changelog entry (type: `methodology_release`) describing
  the error and its expected impact range.
- Within 48 hours: public statement acknowledging the error, linking to the
  changelog, and providing a timeline for the corrected methodology.
- Direct outreach to all academic collaborators (CCI, CEOBS, Queen Mary,
  Lancaster, Oregon State, IGGAW).
- When the corrected methodology is approved: new version tag (e.g., v1.1),
  new PDF, recomputation of all affected estimates, changelog entry for the
  new version.

### Postmortem template
See [Postmortem Template](#postmortem-template). Must include: the exact
equation or parameter that was wrong, the corrected version, the aggregate
impact on published totals, and changes to the review process.

---

## Scenario 4: Source Citation Broken (URL Dead, Paper Retracted)

A source in a provenance chain becomes unavailable — the URL returns 404,
the paper is retracted, or the data provider removes an API endpoint.

### Detection signals
- Automated link checker (scheduled weekly) flags a dead URL in
  `provenance_records` or `sources`
- User report that a citation link is broken
- Data provider announces API deprecation
- Academic paper retraction notice

### Immediate actions (within 72 hours)
1. Technical Lead checks whether the source content was archived (Wayback
   Machine, `content_hash` in the `sources` table can verify integrity of
   any cached copy).
2. If archived copy exists and `content_hash` matches: update the
   `sources.identifier` URL to point to the archive. Log the change as an
   `editorial_decision` changelog entry.
3. If no archive exists:
   - If the source was one of multiple inputs to the provenance chain:
     note the broken link in the provenance record's `notes` field. The
     estimate remains valid if other sources still support it.
   - If the source was the sole basis for an estimate: flag the event for
     re-review. The confidence label may need to be downgraded.
4. If a paper is **retracted** (not just offline): treat this as potential
   grounds for re-evaluating any estimate that relied on it. Escalate to
   SSC Liaison if the retracted paper is a methodological source (e.g.,
   an emission factor reference).

### Communication plan
- Changelog entry for each affected event (type: `editorial_decision`).
- No public statement needed unless >5 events are affected simultaneously.

---

## Scenario 5: Hostile Media Attack on Credibility

A media outlet, government spokesperson, or social media campaign attacks
the project's credibility — claiming the numbers are fabricated, biased,
or politically motivated.

### Detection signals
- Media monitoring (Google Alerts, social media)
- Direct contact from journalists requesting comment
- Spike in negative traffic or feedback

### Immediate actions (within 24 hours)
1. Communications Lead assesses the specific claims being made.
2. Technical Lead prepares a factual rebuttal for each specific claim,
   grounded in:
   - The provenance chain for any cited event
   - The methodology PDF section and equation numbers
   - The raw satellite data (FIRMS, Sentinel-2) that anyone can independently
     verify
3. **Do not engage in back-and-forth on social media.** Respond once with
   facts, then stop.
4. **Do not alter any data or estimates in response to political pressure.**
   The data speaks for itself through its provenance chains.

### Communication plan
- Publish a response on the project's own channels (blog, FAQ) within 48
  hours. Link to specific provenance chains and methodology sections.
- Update the FAQ page with the specific critique and the factual response.
- If the attack is from a state actor: see also Scenario 7.
- Brief academic collaborators so they are prepared if contacted.

### What NOT to do
- Do not remove or modify any events or estimates.
- Do not engage with anonymous social media accounts.
- Do not speculate about the attacker's motives.
- Do not use language that frames the project as an adversarial tool
  (see CLAUDE.md: "visibility tool, not accountability tool").

---

## Scenario 6: Legal Threat Related to "Ecocide" Framing

A legal threat arrives claiming the project's data is being used (or could
be used) to support "ecocide" charges, war crimes claims, or similar legal
proceedings. WCED does not make legal claims — but we must have a response
ready.

### Detection signals
- Formal legal communication (cease and desist, subpoena, legal letter)
- Media reports that WCED data is cited in legal filings
- Advisory from collaborating institutions about legal risk

### Immediate actions (within 24 hours)
1. Incident Lead escalates to the SSC and any institutional legal counsel
   immediately. Do not respond to the legal communication without legal
   advice.
2. Preserve all data, logs, and communications. Implement a litigation hold
   if advised by counsel.
3. Review all public-facing copy to confirm it does not contain:
   - The word "ecocide"
   - Attribution of emissions to specific belligerents (unless the data
     unambiguously supports it and legal counsel approves)
   - Language that frames emissions as "crimes" or "violations"
4. Confirm the following are prominently stated:
   - "This is a visibility and transparency tool, not an accountability tool"
   - "Emission estimates are probability distributions with stated uncertainty"
   - "All data is derived from public satellite sources"
   - "CC-BY 4.0 data license — downstream use is the responsibility of the user"

### Communication plan
- No public statement until legal counsel advises.
- If forced to respond publicly: restate the project's mission and data
  licensing terms. Do not comment on any legal proceedings.
- Brief all collaborating institutions.

### What NOT to do
- Do not remove data, events, or estimates.
- Do not alter methodology or estimates under legal pressure.
- Do not destroy any communications or logs.
- Do not make any public statement about ongoing legal matters without
  legal counsel review.

---

## Scenario 7: State Actor Pressure to Remove an Event

A government (Iranian, Israeli, US, Gulf state, or other) pressures the
project to remove, alter, or suppress a specific event or set of events.

### Detection signals
- Formal government communication demanding removal
- Informal pressure through institutional channels
- Hosting provider or domain registrar receives a takedown request
- Collaborating institution pressured to withdraw support

### Immediate actions (within 24 hours)
1. Incident Lead documents the pressure: who, what, when, through what
   channel. Store this documentation outside the pressuring jurisdiction
   if possible.
2. SSC Liaison convenes an emergency SSC meeting.
3. Review the targeted event(s) for factual accuracy — not because the
   pressure demands it, but because we should always be confident in our
   data. If the event is wrong, follow Scenario 1. If the event is
   correct, it stays.
4. If hosting infrastructure is threatened: activate contingency mirrors.
   The entire project (code, data, methodology) should be reproducible
   from the git repository.

### Communication plan
- If the pressure is public: respond with a statement reaffirming editorial
  independence and citing the provenance chain for the targeted event(s).
- If the pressure is private: do not disclose the specific source of
  pressure, but publish a general statement affirming editorial independence.
- Brief all collaborating institutions and academic partners.
- Notify press freedom and academic freedom organizations if appropriate.

### What NOT to do
- Do not remove, alter, or suppress any event in response to political
  pressure. Every event stands or falls on its provenance chain, not on
  political convenience.
- Do not reveal the identity of editorial reviewers if they could face
  personal risk.

---

## Scenario 8: Data Feed Breakage (FIRMS Down, ACLED API Outage)

A data source the pipeline depends on becomes unavailable — API outage,
rate limit changes, authentication failure, or permanent discontinuation.

### Detection signals
- Pipeline run fails with ingestion errors (Prefect flow failure)
- Structured logs show repeated HTTP 5xx or 429 from a data source
- Data provider announces planned maintenance or discontinuation
- `pipeline_runs` table shows consecutive failures for a flow

### Immediate actions
1. Technical Lead determines scope: which data source, how long it's been
   down, and which pipeline stages are affected.
2. If the outage is temporary (< 48 hours):
   - Pipeline continues with cached data for the affected source.
   - Dashboard displays a banner: "Data from [source] is delayed. Last
     successful update: [timestamp]."
   - No events are published using stale data without reviewer awareness.
3. If the outage is extended (> 48 hours):
   - Evaluate alternative data sources (e.g., VIIRS if MODIS is down).
   - Downgrade confidence labels for new events that lack the missing
     source's corroboration.
   - Changelog entry noting reduced data coverage.
4. If a source is permanently discontinued:
   - SSC Liaison convenes a methodology review to evaluate replacement
     sources.
   - Existing estimates that used the discontinued source remain valid —
     they reference the source as it existed at the time.
   - New estimates must use the approved replacement.

### Communication plan
- Dashboard banner for any outage > 6 hours.
- Changelog entry for any outage > 48 hours or any permanent discontinuation.
- No external communication needed for transient outages < 48 hours.

---

## Scenario 9: AI Model Output Quality Degradation

An AI model used in the pipeline (Claude for severity extraction, ViT/CLIP
for damage classification) begins producing lower-quality outputs — higher
error rates, hallucinated classifications, or outputs that fail validation.

### Detection signals
- Provenance records show increased `CLAIMED` or `SUSPECTED` confidence
  labels where `CONFIRMED` or `VERIFIED` was previously typical
- Structured logs show increased validation failures in `wced/ai/` wrappers
- Model provider announces a version change or deprecation
- Manual review of recent AI outputs reveals quality decline
- Automated drift detection (if implemented) triggers an alert

### Immediate actions (within 24 hours)
1. Technical Lead quantifies the degradation: compare recent AI output
   distributions against a baseline period.
2. If the degradation affects published events:
   - Flag affected events for re-review.
   - Do not auto-retract — the AI output was one input to the provenance
     chain, not the sole authority (per CLAUDE.md: "AI is never the final
     authority on a number").
3. If the model provider changed the model version:
   - Pin to the last known-good version if the API supports it.
   - Run the methodology compliance tests (`tests/methodology/`) against
     the new version.
   - Do not deploy the new version until tests pass.
4. If the model is unavailable:
   - Pipeline falls back to deterministic-only processing. Events that
     require AI classification are held in `PENDING_REVIEW` until the model
     is restored.
   - No event is published with a gap in its provenance chain.

### Communication plan
- Internal: alert the editorial board that AI-assisted classifications may
  need additional manual review.
- External: no public statement unless published estimates are revised as a
  result. If they are, follow Scenario 2.

### What NOT to do
- Do not let degraded AI outputs reach the database without a provenance
  record noting the degradation.
- Do not switch to a different model family without SSC approval — the
  methodology version is tied to specific model capabilities.
- Do not trust AI outputs over deterministic calculations (per CLAUDE.md:
  "If an AI output disagrees with a deterministic calculation: trust the
  deterministic one").

---

## Postmortem Template

Every incident at severity "retraction" or above gets a written postmortem
stored in `docs/postmortems/YYYY-MM-DD-<slug>.md`.

```markdown
# Postmortem: <short title>

**Date:** YYYY-MM-DD
**Severity:** retraction | estimate-revision | methodology-correction | operational
**Incident Lead:** <name>
**Status:** draft | reviewed | published

## Summary
One paragraph: what happened, what was the impact, is it resolved.

## Timeline
| Time (UTC) | Event |
|------------|-------|
| HH:MM | Detection signal received |
| HH:MM | Incident Lead assigned |
| HH:MM | ... |
| HH:MM | Resolution confirmed |

## Root cause
What specifically went wrong. Cite code paths, data sources, or
methodology sections.

## Impact
- Events affected: <list of event IDs>
- Estimates revised: <old p50 → new p50 for each>
- Aggregate impact: <change to headline totals>
- External citations affected: <list if known>

## What went well
- Detection was fast / slow
- Response followed / deviated from the runbook

## What went wrong
- The root cause, and why it wasn't caught earlier

## Action items
| Action | Owner | Due date | Status |
|--------|-------|----------|--------|
| ... | ... | YYYY-MM-DD | open / done |

## Changelog entries created
- <link to changelog entry 1>
- <link to changelog entry 2>
```

---

## Severity Classification

| Severity | Definition | Response time | Postmortem required |
|----------|-----------|---------------|---------------------|
| **Critical** | Factually wrong event published, or methodology error affecting >10 events | 2 hours | Yes |
| **High** | Estimate wrong by >2x, data feed down >48h, AI degradation affecting published events | 24 hours | Yes |
| **Medium** | Broken citation, estimate revision <2x, transient data feed outage | 72 hours | Optional |
| **Low** | Cosmetic error, non-critical link broken, AI degradation not affecting published events | 1 week | No |

---

## Escalation Path

1. **Anyone** detects a signal → notifies Incident Lead
2. **Incident Lead** classifies severity and assigns roles
3. **Critical or High** → SSC Liaison notifies the Scientific Steering Committee within 4 hours
4. **Any legal dimension** → SSC Liaison engages institutional legal counsel immediately
5. **Any retraction** → Editorial Reviewer executes via `ReviewQueue.retract()` — the only code path for retractions
