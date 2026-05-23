# War Carbon Emissions Dashboard (WCED)

A near-real-time, publicly auditable dashboard quantifying CO₂ emissions from
oil and fuel infrastructure fires during the 2026 Iran–US–Israel war, using
only public satellite data and peer-reviewed emission factors.

## Academic Research Tool Disclaimer

**This is an academic research tool.** It is not a legal accountability
instrument, not a real-time alert system, and not a tool for predicting future
events. All emission estimates are distributions with explicit uncertainty bounds
(5th/50th/95th percentile). No incident appears on the dashboard until it has
≥2 independent sources or satellite confirmation. Methodology is versioned and
peer-reviewed; numbers are reproducible from a git-tracked snapshot.

**Attribution of responsibility to specific belligerents is out of scope.**

## Methodology

Full methodology: [`methodology/v1.0.pdf`](methodology/v1.0.pdf) *(placeholder —
approved by Scientific Steering Committee before publication)*

Emission factors and parameter distributions: [`data/emission_factors.yaml`](data/emission_factors.yaml)

## Quick Start

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and set up
git clone <repo>
cd war-carbon-dashboard
uv sync
cp .env.example .env  # fill in API keys

# Run the API (dev)
uv run uvicorn wced.api.main:app --reload

# Run the CLI
uv run wced --help

# Tests
uv run pytest tests/ -v
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
