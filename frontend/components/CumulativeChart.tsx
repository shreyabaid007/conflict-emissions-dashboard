"use client";

import {
  useRef,
  useState,
  useMemo,
  useCallback,
  useEffect,
  type MouseEvent as ReactMouseEvent,
} from "react";
import type { TimeseriesPoint, MajorEvent } from "@/lib/api";
import { formatCO2Compact } from "@/lib/format";

interface CumulativeChartProps {
  points: TimeseriesPoint[];
  majorEvents: MajorEvent[];
  onBrushChange?: (range: [string, string] | null) => void;
}

const MARGIN = { top: 20, right: 20, bottom: 60, left: 72 };

function toX(
  date: string,
  minDate: number,
  maxDate: number,
  width: number
): number {
  const t = new Date(date).getTime();
  const range = maxDate - minDate || 1;
  return MARGIN.left + ((t - minDate) / range) * width;
}

function toY(val: number, maxVal: number, height: number): number {
  if (maxVal === 0) return MARGIN.top + height;
  return MARGIN.top + height - (val / maxVal) * height;
}

export function CumulativeChart({
  points,
  majorEvents,
  onBrushChange,
}: CumulativeChartProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [dimensions, setDimensions] = useState({ width: 800, height: 340 });
  const [brushStart, setBrushStart] = useState<number | null>(null);
  const [brushEnd, setBrushEnd] = useState<number | null>(null);
  const [brushing, setBrushing] = useState(false);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  useEffect(() => {
    const el = svgRef.current?.parentElement;
    if (!el) return;
    const obs = new ResizeObserver((entries) => {
      const { width } = entries[0].contentRect;
      setDimensions({ width, height: 340 });
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  const chartW = dimensions.width - MARGIN.left - MARGIN.right;
  const chartH = dimensions.height - MARGIN.top - MARGIN.bottom;

  const { minDate, maxDate, maxVal, dates } = useMemo(() => {
    if (points.length === 0)
      return { minDate: 0, maxDate: 1, maxVal: 1, dates: [] as number[] };
    const ds = points.map((p) => new Date(p.date).getTime());
    return {
      minDate: ds[0],
      maxDate: ds[ds.length - 1],
      maxVal: Math.max(...points.map((p) => p.cumulative_p95)) * 1.05,
      dates: ds,
    };
  }, [points]);

  const bandPath = useMemo(() => {
    if (points.length === 0) return "";
    const upper = points
      .map(
        (p) =>
          `${toX(p.date, minDate, maxDate, chartW)},${toY(p.cumulative_p95, maxVal, chartH)}`
      )
      .join(" L");
    const lower = [...points]
      .reverse()
      .map(
        (p) =>
          `${toX(p.date, minDate, maxDate, chartW)},${toY(p.cumulative_p5, maxVal, chartH)}`
      )
      .join(" L");
    return `M${upper} L${lower} Z`;
  }, [points, minDate, maxDate, maxVal, chartW, chartH]);

  const medianPath = useMemo(() => {
    if (points.length === 0) return "";
    return points
      .map((p, i) => {
        const x = toX(p.date, minDate, maxDate, chartW);
        const y = toY(p.cumulative_p50, maxVal, chartH);
        return `${i === 0 ? "M" : "L"}${x},${y}`;
      })
      .join(" ");
  }, [points, minDate, maxDate, maxVal, chartW, chartH]);

  const yTicks = useMemo(() => {
    const count = 5;
    const step = maxVal / count;
    return Array.from({ length: count + 1 }, (_, i) => i * step);
  }, [maxVal]);

  const xTicks = useMemo(() => {
    if (points.length <= 1) return points.map((p) => p.date);
    // Each rotated label needs ~70px of horizontal room
    const maxTicks = Math.max(2, Math.floor(chartW / 80));
    const count = Math.min(maxTicks, points.length);
    const step = Math.max(1, Math.floor((points.length - 1) / (count - 1 || 1)));
    const ticks: string[] = [];
    for (let i = 0; i < points.length; i += step) {
      ticks.push(points[i].date);
    }
    if (ticks[ticks.length - 1] !== points[points.length - 1].date) {
      ticks.push(points[points.length - 1].date);
    }
    return ticks;
  }, [points, chartW]);

  const getIndexFromMouseX = useCallback(
    (clientX: number) => {
      if (!svgRef.current || points.length === 0) return null;
      const rect = svgRef.current.getBoundingClientRect();
      const x = clientX - rect.left - MARGIN.left;
      const fraction = x / chartW;
      const idx = Math.round(fraction * (points.length - 1));
      return Math.max(0, Math.min(points.length - 1, idx));
    },
    [points, chartW]
  );

  const handleMouseDown = useCallback(
    (e: ReactMouseEvent) => {
      const idx = getIndexFromMouseX(e.clientX);
      if (idx === null) return;
      setBrushing(true);
      setBrushStart(idx);
      setBrushEnd(idx);
    },
    [getIndexFromMouseX]
  );

  const handleMouseMove = useCallback(
    (e: ReactMouseEvent) => {
      const idx = getIndexFromMouseX(e.clientX);
      if (idx === null) return;
      setHoverIndex(idx);
      if (brushing) {
        setBrushEnd(idx);
      }
    },
    [getIndexFromMouseX, brushing]
  );

  const handleMouseUp = useCallback(() => {
    if (brushing && brushStart !== null && brushEnd !== null) {
      const lo = Math.min(brushStart, brushEnd);
      const hi = Math.max(brushStart, brushEnd);
      if (lo !== hi && onBrushChange) {
        onBrushChange([points[lo].date, points[hi].date]);
      }
    }
    setBrushing(false);
  }, [brushing, brushStart, brushEnd, points, onBrushChange]);

  const handleDoubleClick = useCallback(() => {
    setBrushStart(null);
    setBrushEnd(null);
    onBrushChange?.(null);
  }, [onBrushChange]);

  const brushRect = useMemo(() => {
    if (brushStart === null || brushEnd === null) return null;
    const lo = Math.min(brushStart, brushEnd);
    const hi = Math.max(brushStart, brushEnd);
    if (lo === hi) return null;
    const x1 = toX(points[lo].date, minDate, maxDate, chartW);
    const x2 = toX(points[hi].date, minDate, maxDate, chartW);
    return { x: x1, width: x2 - x1 };
  }, [brushStart, brushEnd, points, minDate, maxDate, chartW]);

  const hoverPoint = hoverIndex !== null ? points[hoverIndex] : null;

  if (points.length === 0) {
    return (
      <p className="py-16 text-center text-sm text-slate-600">
        No timeseries data available.
      </p>
    );
  }

  return (
    <div className="relative w-full">
      <svg
        ref={svgRef}
        width={dimensions.width}
        height={dimensions.height}
        className="select-none"
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={() => {
          setHoverIndex(null);
          if (brushing) handleMouseUp();
        }}
        onDoubleClick={handleDoubleClick}
      >
        <defs>
          <linearGradient id="bandGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#f97316" stopOpacity="0.15" />
            <stop offset="100%" stopColor="#f97316" stopOpacity="0.03" />
          </linearGradient>
          <linearGradient id="lineGrad" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#fb923c" />
            <stop offset="100%" stopColor="#f97316" />
          </linearGradient>
        </defs>

        {/* Y grid + labels */}
        {yTicks.map((val) => {
          const y = toY(val, maxVal, chartH);
          return (
            <g key={val}>
              <line
                x1={MARGIN.left}
                x2={MARGIN.left + chartW}
                y1={y}
                y2={y}
                stroke="rgba(255,255,255,0.04)"
                strokeWidth={1}
              />
              <text
                x={MARGIN.left - 12}
                y={y + 3}
                textAnchor="end"
                className="text-[10px]"
                fill="#64748b"
              >
                {formatCO2Compact(val)}
              </text>
            </g>
          );
        })}

        {/* X labels */}
        {xTicks.map((date) => {
          const x = toX(date, minDate, maxDate, chartW);
          const y = MARGIN.top + chartH + 18;
          return (
            <text
              key={date}
              x={x}
              y={y}
              textAnchor="end"
              transform={`rotate(-35 ${x} ${y})`}
              className="text-[10px]"
              fill="#64748b"
            >
              {new Date(date).toLocaleDateString("en-US", {
                month: "short",
                day: "numeric",
              })}
            </text>
          );
        })}

        {/* Uncertainty band */}
        <path d={bandPath} fill="url(#bandGrad)" />

        {/* Median line */}
        <path
          d={medianPath}
          fill="none"
          stroke="url(#lineGrad)"
          strokeWidth={2}
          className="chart-glow"
        />

        {/* Major event markers */}
        {majorEvents.map((evt) => {
          const x = toX(evt.date, minDate, maxDate, chartW);
          return (
            <g key={evt.event_id}>
              <line
                x1={x}
                x2={x}
                y1={MARGIN.top}
                y2={MARGIN.top + chartH}
                stroke="#ef4444"
                strokeWidth={1}
                strokeDasharray="4 3"
                opacity={0.5}
              />
              <text
                x={x + 4}
                y={MARGIN.top + 12}
                className="text-[9px] font-medium"
                fill="#ef4444"
                opacity={0.8}
              >
                {evt.label}
              </text>
            </g>
          );
        })}

        {/* Brush overlay */}
        {brushRect && (
          <rect
            x={brushRect.x}
            y={MARGIN.top}
            width={brushRect.width}
            height={chartH}
            fill="#f97316"
            opacity={0.08}
          />
        )}

        {/* Hover crosshair */}
        {hoverPoint && !brushing && (
          <>
            <line
              x1={toX(hoverPoint.date, minDate, maxDate, chartW)}
              x2={toX(hoverPoint.date, minDate, maxDate, chartW)}
              y1={MARGIN.top}
              y2={MARGIN.top + chartH}
              stroke="rgba(255,255,255,0.1)"
              strokeWidth={1}
            />
            <circle
              cx={toX(hoverPoint.date, minDate, maxDate, chartW)}
              cy={toY(hoverPoint.cumulative_p50, maxVal, chartH)}
              r={4}
              fill="#f97316"
              stroke="#1e293b"
              strokeWidth={2}
            />
          </>
        )}
      </svg>

      {/* Hover tooltip */}
      {hoverPoint && !brushing && hoverIndex !== null && (
        <div
          className="pointer-events-none absolute z-30 rounded-lg border border-white/[0.08] bg-slate-800/95 px-4 py-3 text-xs shadow-xl backdrop-blur-sm"
          style={{
            left: Math.min(
              toX(hoverPoint.date, minDate, maxDate, chartW),
              dimensions.width - 220
            ),
            top: MARGIN.top,
          }}
        >
          <p className="font-medium text-slate-200">
            {new Date(hoverPoint.date).toLocaleDateString("en-US", {
              month: "long",
              day: "numeric",
              year: "numeric",
            })}
          </p>
          <p className="mt-1.5 tabular-nums text-orange-400 font-semibold">
            Median: {formatCO2Compact(hoverPoint.cumulative_p50)}
          </p>
          <p className="tabular-nums text-slate-500">
            90% CI: {formatCO2Compact(hoverPoint.cumulative_p5)} –{" "}
            {formatCO2Compact(hoverPoint.cumulative_p95)}
          </p>
        </div>
      )}

      <p className="mt-2 text-center text-[10px] text-slate-600">
        Click and drag to select a date range. Double-click to reset.
      </p>
    </div>
  );
}
