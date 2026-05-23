interface UncertaintyBarProps {
  p5: number;
  p50: number;
  p95: number;
  unit?: string;
}

export function UncertaintyBar({ p5, p50, p95, unit = "tCO₂e" }: UncertaintyBarProps) {
  const range = p95 - p5;
  const medianPct = range > 0 ? ((p50 - p5) / range) * 100 : 50;

  return (
    <div className="space-y-1.5">
      <div className="relative h-2 w-full overflow-hidden rounded-full bg-white/[0.04]">
        <div
          className="absolute inset-y-0 rounded-full bg-orange-500/20"
          style={{ left: "0%", right: "0%" }}
        />
        <div
          className="absolute top-0 h-2 w-0.5 rounded-full bg-orange-400"
          style={{ left: `${medianPct}%` }}
        />
      </div>
      <div className="flex justify-between text-xs tabular-nums text-slate-500">
        <span>
          {p5.toLocaleString()} {unit}
        </span>
        <span className="font-medium text-orange-400">
          {p50.toLocaleString()} {unit}
        </span>
        <span>
          {p95.toLocaleString()} {unit}
        </span>
      </div>
      <div className="flex justify-between text-[10px] text-slate-600">
        <span>5th</span>
        <span>50th</span>
        <span>95th</span>
      </div>
    </div>
  );
}
