"use client";

import Link from "next/link";
import type { EventSummary } from "@/lib/api";
import { formatCO2Compact } from "@/lib/format";
import { CONFIDENCE_COLORS } from "@/lib/constants";

interface RecentEventsListProps {
  events: EventSummary[];
}

export function RecentEventsList({ events }: RecentEventsListProps) {
  if (events.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-slate-600">
        No published events yet.
      </p>
    );
  }

  return (
    <ul className="divide-y divide-white/[0.04]">
      {events.map((evt) => {
        const color = CONFIDENCE_COLORS[evt.confidence] ?? "#a3a3a3";
        return (
          <li key={evt.id}>
            <Link
              href={`/event/${evt.id}`}
              className="flex items-center gap-3 rounded-lg px-3 py-3 transition-colors hover:bg-white/[0.03]"
            >
              <span
                className="h-2.5 w-2.5 shrink-0 rounded-full"
                style={{
                  backgroundColor: color,
                  boxShadow: `0 0 6px ${color}60`,
                }}
                title={evt.confidence}
              />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-slate-200">
                  {evt.facility_name}
                </span>
                <span className="block text-xs text-slate-500">
                  {evt.date}
                  <span className="mx-1.5 text-slate-700">&middot;</span>
                  <span className="capitalize">
                    {evt.confidence.toLowerCase()}
                  </span>
                </span>
              </span>
              <span className="shrink-0 text-right">
                {evt.emission_p50 != null ? (
                  <span className="text-sm tabular-nums font-medium text-orange-400">
                    {formatCO2Compact(evt.emission_p50)}
                  </span>
                ) : (
                  <span className="text-xs text-slate-600">pending</span>
                )}
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
