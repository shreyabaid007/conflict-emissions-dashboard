# War Carbon Emissions Dashboard (WCED)

A near-real-time, publicly auditable dashboard quantifying CO₂ emissions from
oil and fuel infrastructure fires during the 2026 Iran–US–Israel war, using
only public satellite data and peer-reviewed emission factors.

**V1 scope:** Oil and fuel infrastructure fire emissions in Iran and the Gulf
region (28 Feb 2026 – present). Current methodology version: **v1.0.5**.

**Headline number (as of 2026-05-24):** ~75.6 kt CO₂e cumulative (p50) across
7 Iranian facilities, based on 47 detected fire events (27 with emission
estimates, 2 fully reconciled FRP vs. inventory).

## Academic Research Tool Disclaimer

**This is an academic research tool.** It is not a legal accountability
instrument, not a real-time alert system, and not a tool for predicting future
events. All emission estimates are distributions with explicit uncertainty bounds
(5th/50th/95th percentile). No incident appears on the dashboard until it has
≥2 independent sources or satellite confirmation. Methodology is versioned and
peer-reviewed; numbers are reproducible from a git-tracked snapshot.

**Attribution of responsibility to specific belligerents is out of scope.**

## Methodology

Current methodology: [`methodology/v1.0.pdf`](methodology/v1.0.pdf) (base),
superseded by v1.0.5 (baseline subtraction, fraction-destroyed recalibration).
See [`methodology/CHANGELOG.md`](methodology/CHANGELOG.md) for the full
version history.

Emission factors and parameter distributions: [`data/emission_factors.yaml`](data/emission_factors.yaml)

## Quick Start

```bash
# Install uv and just
curl -LsSf https://astral.sh/uv/install.sh | sh
brew install just  # or cargo install just

# Clone and set up
git clone https://github.com/shreyabaid007/conflict-emission-tracker
cd conflict-emission-tracker
uv sync
cp .env.example .env  # fill in API keys

# Start the dev stack (Postgres, Redis, MinIO, Prefect, API)
just bootstrap        # docker compose up + migrate

# Load facility registry
just facility-load

# Ingest today's FIRMS data and run detection
just ingest
just detect

# Run the CLI
uv run wced --help

# Tests
just test
```

## Data Sources

| Source | Purpose | License |
|--------|---------|---------|
| NASA FIRMS / VIIRS | Fire radiative power (FRP) hotspot detection | Public domain |
| Sentinel-2 (Planetary Computer) | Optical damage confirmation | Copernicus / ESA |
| TROPOMI / Sentinel-5P | Top-down CO₂/SO₂ column validation | Copernicus / ESA |
| ACLED | Conflict event corroboration | CC-BY |
| OpenInfraMap | Facility geometries | ODbL |

## License

Code: MIT — see [LICENSE](LICENSE)
Data outputs: CC-BY 4.0 — see [DATA_LICENSE](DATA_LICENSE)
