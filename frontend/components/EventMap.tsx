"use client";

import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import type { EventSummary } from "@/lib/api";
import { CONFIDENCE_COLORS } from "@/lib/constants";
import { formatCO2Compact } from "@/lib/format";

interface EventMapProps {
  events: EventSummary[];
  center?: [number, number];
  zoom?: number;
  className?: string;
}

function emissionRadius(p50: number | null): number {
  if (!p50 || p50 <= 0) return 6;
  return Math.max(6, Math.min(40, 4 + Math.sqrt(p50) * 0.2));
}

interface SelectedEvent {
  evt: EventSummary;
  pixel: { x: number; y: number };
}

export function EventMap({
  events,
  center = [53.0, 32.5],
  zoom = 5,
  className = "",
}: EventMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markersRef = useRef<maplibregl.Marker[]>([]);
  const [selected, setSelected] = useState<SelectedEvent | null>(null);

  const withEstimates = events.filter(
    (e) => e.emission_p50 != null && e.emission_p50 > 0,
  );

  useEffect(() => {
    if (!containerRef.current) return;

    if (mapRef.current) {
      mapRef.current.remove();
      mapRef.current = null;
    }

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: {
        version: 8,
        sources: {
          carto: {
            type: "raster",
            tiles: [
              "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
              "https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
              "https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
            ],
            tileSize: 256,
            attribution:
              '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
          },
        },
        layers: [
          {
            id: "carto-dark",
            type: "raster",
            source: "carto",
          },
        ],
      },
      center,
      zoom,
    });

    map.addControl(new maplibregl.NavigationControl(), "top-right");
    mapRef.current = map;

    map.on("click", () => {
      setSelected(null);
    });

    map.on("movestart", () => {
      setSelected(null);
    });

    return () => {
      markersRef.current.forEach((m) => m.remove());
      markersRef.current = [];
      map.remove();
      mapRef.current = null;
    };
  }, [center, zoom]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];

    withEstimates.forEach((evt) => {
      const size = emissionRadius(evt.emission_p50);
      const color = CONFIDENCE_COLORS[evt.confidence] ?? "#a3a3a3";

      const el = document.createElement("div");
      el.style.width = `${size}px`;
      el.style.height = `${size}px`;
      el.style.cursor = "pointer";

      const dot = document.createElement("div");
      dot.style.width = "100%";
      dot.style.height = "100%";
      dot.style.borderRadius = "50%";
      dot.style.backgroundColor = color;
      dot.style.opacity = "0.85";
      dot.style.border = "1.5px solid rgba(255,255,255,0.3)";
      dot.style.boxShadow = `0 0 ${size}px ${color}80, 0 0 ${size * 2}px ${color}30`;
      dot.style.transition = "transform 0.2s, box-shadow 0.2s";
      dot.style.transformOrigin = "center center";
      el.appendChild(dot);

      el.addEventListener("mouseenter", () => {
        dot.style.transform = "scale(1.3)";
        dot.style.boxShadow = `0 0 ${size * 1.5}px ${color}, 0 0 ${size * 3}px ${color}60`;
      });
      el.addEventListener("mouseleave", () => {
        dot.style.transform = "scale(1)";
        dot.style.boxShadow = `0 0 ${size}px ${color}80, 0 0 ${size * 2}px ${color}30`;
      });

      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const rect = containerRef.current!.getBoundingClientRect();
        const markerRect = el.getBoundingClientRect();
        setSelected({
          evt,
          pixel: {
            x: markerRect.left + markerRect.width / 2 - rect.left,
            y: markerRect.top - rect.top,
          },
        });
      });

      const marker = new maplibregl.Marker({ element: el, anchor: "center" })
        .setLngLat([evt.longitude, evt.latitude])
        .addTo(map);

      markersRef.current.push(marker);
    });
  }, [withEstimates]);

  const p50s = withEstimates
    .map((e) => e.emission_p50!)
    .filter((v) => v > 0);
  const minP50 = p50s.length > 0 ? Math.min(...p50s) : 200;
  const maxP50 = p50s.length > 0 ? Math.max(...p50s) : 20000;

  return (
    <div className={`relative h-full ${className}`}>
      <div
        ref={containerRef}
        className="h-full w-full"
        style={{ minHeight: "460px" }}
      />

      {/* Custom info card — no map panning */}
      {selected && (
        <div
          className="absolute z-30 w-64 rounded-xl border border-white/[0.08] bg-slate-800/95 shadow-2xl backdrop-blur-sm"
          style={{
            left: `${Math.min(selected.pixel.x - 128, (containerRef.current?.offsetWidth ?? 600) - 270)}px`,
            top: `${Math.max(selected.pixel.y - 180, 8)}px`,
          }}
        >
          <div className="p-4">
            <div className="flex items-start justify-between">
              <div className="font-semibold text-[13px] text-slate-100 leading-tight pr-4">
                {selected.evt.facility_name}
              </div>
              <button
                onClick={() => setSelected(null)}
                className="shrink-0 text-slate-500 hover:text-slate-200 text-lg leading-none -mt-0.5"
              >
                &times;
              </button>
            </div>
            <div className="mt-1 text-[11px] text-slate-400">
              {selected.evt.date}
            </div>
            <div className="mt-3 pt-3 border-t border-white/[0.06]">
              {selected.evt.emission_p50 != null ? (
                <span className="text-orange-400 font-semibold text-sm">
                  {formatCO2Compact(selected.evt.emission_p50)}
                </span>
              ) : (
                <span className="text-slate-500 text-sm">Estimate pending</span>
              )}
            </div>
            <div className="mt-2 flex items-center gap-1.5">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{
                  backgroundColor: CONFIDENCE_COLORS[selected.evt.confidence] ?? "#a3a3a3",
                }}
              />
              <span className="uppercase text-[10px] tracking-wide text-slate-400">
                {selected.evt.confidence}
              </span>
            </div>
            <a
              href={`/event/${selected.evt.id}`}
              className="mt-3 block text-center text-[11px] text-orange-400 hover:text-orange-300 transition-colors"
            >
              View details &rarr;
            </a>
          </div>
        </div>
      )}

      {/* Legend */}
      <div className="absolute bottom-4 left-4 rounded-xl border border-white/[0.08] bg-slate-900/90 px-4 py-3 text-[10px] shadow-lg backdrop-blur-sm">
        <p className="mb-2 text-[11px] font-semibold text-slate-300">Legend</p>
        <p className="mb-1.5 text-[9px] font-medium uppercase tracking-widest text-slate-500">
          Confidence
        </p>
        <div className="flex gap-4 mb-3">
          {Object.entries(CONFIDENCE_COLORS).map(([label, color]) => (
            <span key={label} className="flex items-center gap-2 text-slate-300">
              <span
                className="inline-block h-3 w-3 rounded-full border border-white/20"
                style={{ backgroundColor: color, boxShadow: `0 0 8px ${color}80` }}
              />
              <span className="capitalize text-[11px]">{label.toLowerCase()}</span>
            </span>
          ))}
        </div>
        <p className="mb-1 text-[9px] font-medium uppercase tracking-widest text-slate-500">
          Circle size (median tCO&#8322;e)
        </p>
        <div className="flex items-center gap-4 mb-2">
          <span className="flex items-center gap-1.5 text-slate-400">
            <span
              className="inline-block rounded-full bg-slate-500"
              style={{
                width: `${emissionRadius(minP50)}px`,
                height: `${emissionRadius(minP50)}px`,
              }}
            />
            ~{formatCO2Compact(minP50)}
          </span>
          <span className="flex items-center gap-1.5 text-slate-400">
            <span
              className="inline-block rounded-full bg-slate-500"
              style={{
                width: `${emissionRadius(maxP50)}px`,
                height: `${emissionRadius(maxP50)}px`,
              }}
            />
            ~{formatCO2Compact(maxP50)}
          </span>
        </div>
        <p className="text-slate-600 italic">
          Only events with emission estimates shown
        </p>
      </div>
    </div>
  );
}
