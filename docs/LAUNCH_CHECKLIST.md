# WCED Public Launch Checklist

All items must be true before making the dashboard publicly accessible.
Run `python scripts/launch_check.py` to verify technical items automatically.

---

## Technical

- [ ] Methodology PDF v1.0 published with SSC approval (named reviewers listed)
- [ ] All published events have full provenance chains
- [ ] All emission estimates have Monte Carlo bounds (p5/p50/p95 populated)
- [ ] CI passes including methodology compliance tests
- [ ] OpenAPI documentation complete (all endpoints documented with examples)
- [ ] GitHub repository public with MIT license (code) and CC-BY 4.0 license (data)
- [ ] Replication package downloadable (pinned dependencies, seed data, run instructions)

## Editorial

- [ ] Editorial board members named and listed on the site
- [ ] First 20 events manually reviewed and approved
- [ ] No events with `CONFIRMED` confidence label lack satellite confirmation
- [ ] At least 2 events have TROPOMI top-down cross-check (validation_reports populated)

## Communication

- [ ] Methodology paper preprint posted on SSRN
- [ ] Outreach to CCI, CEOBS, Queen Mary, Lancaster, Oregon State, and IGGAW confirming awareness and feedback opportunity
- [ ] Press release draft reviewed by SSC
- [ ] FAQ addressing likely critiques (overstating, understating, political bias) published

## Operational

- [ ] Incident response runbook documented (what to do if a published event is wrong)
- [ ] Dual-use review completed for all facility coordinates with sub-100m precision
- [ ] Funding source disclosures published on the site
