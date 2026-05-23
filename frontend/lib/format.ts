export function formatCO2(tonnes: number): { value: string; unit: string } {
  if (tonnes >= 1_000_000) {
    return { value: (tonnes / 1_000_000).toFixed(1), unit: "Mt CO₂e" };
  }
  if (tonnes >= 1_000) {
    return { value: (tonnes / 1_000).toFixed(1), unit: "kt CO₂e" };
  }
  return { value: Math.round(tonnes).toLocaleString(), unit: "t CO₂e" };
}

export function formatCO2Compact(tonnes: number): string {
  const { value, unit } = formatCO2(tonnes);
  return `${value} ${unit}`;
}

export function daysSince(start: Date, now: Date = new Date()): number {
  const ms = now.getTime() - start.getTime();
  return Math.floor(ms / (1000 * 60 * 60 * 24));
}
