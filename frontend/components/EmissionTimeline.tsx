"use client";

interface TimelinePoint {
  date: string;
  p5: number;
  p50: number;
  p95: number;
}

interface EmissionTimelineProps {
  data: TimelinePoint[];
}

export function EmissionTimeline({ data }: EmissionTimelineProps) {
  if (data.length === 0) {
    return (
      <p className="text-sm text-slate-600">No timeline data available.</p>
    );
  }

  const maxVal = Math.max(...data.map((d) => d.p95));

  return (
    <div className="space-y-1">
      {data.map((point) => {
        const widthPct = maxVal > 0 ? (point.p95 / maxVal) * 100 : 0;
        const medianPct = maxVal > 0 ? (point.p50 / maxVal) * 100 : 0;

        return (
          <div key={point.date} className="flex items-center gap-3 text-xs">
            <span className="w-20 shrink-0 tabular-nums text-slate-500">
              {point.date}
            </span>
            <div className="relative h-5 flex-1 overflow-hidden rounded bg-white/[0.03]">
              <div
                className="absolute inset-y-0 left-0 rounded bg-orange-500/15"
                style={{ width: `${widthPct}%` }}
              />
              <div
                className="absolute inset-y-0 left-0 w-0.5 bg-orange-400"
                style={{ left: `${medianPct}%` }}
              />
            </div>
            <span className="w-16 shrink-0 text-right tabular-nums text-slate-400">
              {point.p50.toLocaleString()}
            </span>
          </div>
        );
      })}
    </div>
  );
}
