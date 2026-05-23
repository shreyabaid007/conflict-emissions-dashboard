# Contributing

Thank you for your interest in the War Carbon Emissions Dashboard.

**This project is paused at V1.** The codebase is open-sourced for transparency
and reproducibility. We are not actively developing new features, but we welcome
bug reports, methodology corrections, and documentation improvements.

## How to contribute

1. Open an issue describing the problem or improvement before submitting a PR.
2. For methodology questions or corrections, reference the relevant section in
   `methodology/v1.0.pdf` or `methodology/CHANGELOG.md`.
3. Run `just test` before submitting. Aim for no regressions.
4. Follow the coding standards in `CLAUDE.md` — type hints everywhere, Pydantic
   models at module boundaries, `Distribution` (not floats) for emission
   estimates, structured logging via `structlog`.

## What we especially welcome

- Corrections to emission factors or parameter distributions (with citations)
- Improvements to the verification layer (see
  [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md))
- Additional facility registry entries (with source attribution)
- Bug reports with reproduction steps

## Scope

This is a visibility tool, not an accountability instrument. Contributions that
frame the tool for legal weaponization or belligerent attribution are out of
scope.

## License

By contributing, you agree that your contributions will be licensed under the
MIT License (code) and CC-BY 4.0 (data outputs).
