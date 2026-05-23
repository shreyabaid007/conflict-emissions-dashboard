## Summary

<!-- What does this PR do? Link to issue or context if available. -->

## Checklist

### Methodology
- [ ] **No quantification logic changed** — OR — methodology PDF updated and version bumped in `methodology/CHANGELOG.md`
- [ ] Emission factor changes are in `data/emission_factors.yaml`, not hardcoded in Python
- [ ] If a new equation was added: section reference cited in a code comment (e.g., `# methodology/v1.0.pdf §3.2`)

### Tests
- [ ] `pytest tests/ -v` passes locally
- [ ] Coverage on `wced/quantify/` remains ≥ 80%
- [ ] New quantification functions have hand-computed expected values in tests (not just smoke tests)
- [ ] No live API calls in tests (VCR cassettes or mocks used)

### Provenance
- [ ] Every new function that produces a numeric output accepts and propagates a `Provenance` object
- [ ] No AI/LLM output reaches the database without a `ProvenanceRecord`
- [ ] `methodology_version` is set on any new estimate records

### Uncertainty
- [ ] Functions returning emission estimates return a `Distribution` (`.p5`, `.p50`, `.p95`, `.samples`), not a float
- [ ] Monte Carlo seeds are explicit and stored with the estimate

### Code quality
- [ ] `ruff check` and `ruff format --check` pass
- [ ] `mypy --strict` passes on `wced/quantify/` and `wced/provenance/`
- [ ] No `print()` statements — structured logging via `structlog` only

### Sensitive areas
- [ ] No sub-100m facility coordinates added without passing the dual-use review checklist (`docs/DUAL_USE_REVIEW.md`)
- [ ] No casualty figures added (out of scope)
- [ ] Attribution to specific belligerents is aggregated, not individualised
