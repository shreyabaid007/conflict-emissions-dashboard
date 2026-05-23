export const WAR_START = new Date("2026-02-28T00:00:00Z");

export const IRAN_ANNUAL_CO2_MT = 720;
export const IRAN_DAILY_CO2_TONNES = (IRAN_ANNUAL_CO2_MT * 1_000_000) / 365;

export const CONFIDENCE_COLORS: Record<string, string> = {
  CONFIRMED: "#60a5fa",
  VERIFIED: "#fbbf24",
  REPORTED: "#f87171",
};

export const CONFIDENCE_LABELS: Record<string, string> = {
  CONFIRMED:
    "Satellite detection corroborated by at least one independent source (ACLED event, optical confirmation, or multiple FIRMS overpasses).",
  VERIFIED:
    "FIRMS persistent detection with Sentinel-2 optical confirmation or GDELT corroboration. Not yet ACLED-corroborated.",
  REPORTED:
    "FIRMS persistent detection without optical confirmation. Satellite-detected but not independently verified.",
};

export const EPA_CO2_PER_CAR_PER_YEAR = 4.6;
