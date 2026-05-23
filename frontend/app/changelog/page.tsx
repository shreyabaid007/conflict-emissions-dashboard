"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchChangelog, type ChangelogEntryResponse } from "@/lib/api";

type FilterType = "all" | "methodology_release" | "event_retraction" | "editorial_decision";

const FILTER_OPTIONS: { value: FilterType; label: string }[] = [
  { value: "all", label: "All" },
  { value: "methodology_release", label: "Methodology" },
  { value: "event_retraction", label: "Retractions" },
  { value: "editorial_decision", label: "Editorial" },
];

function entryTypeLabel(changeType: string): string {
  switch (changeType) {
    case "methodology_release":
      return "Methodology";
    case "event_retraction":
      return "Retraction";
    case "editorial_decision":
      return "Editorial";
    default:
      return changeType;
  }
}

function entryTypeBadgeClass(changeType: string): string {
  switch (changeType) {
    case "methodology_release":
      return "bg-blue-500/10 text-blue-400 border-blue-500/20";
    case "event_retraction":
      return "bg-red-500/10 text-red-400 border-red-500/20";
    case "editorial_decision":
      return "bg-amber-500/10 text-amber-400 border-amber-500/20";
    default:
      return "bg-white/5 text-slate-400 border-white/10";
  }
}

function entryAnchor(entry: ChangelogEntryResponse, index: number): string {
  if (entry.version_id) return `v-${entry.version_id}`;
  if (entry.event_id) return `evt-${entry.event_id}`;
  return `entry-${index}`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

const FALLBACK_ENTRIES: ChangelogEntryResponse[] = [
  {
    version_id: "v1.0.5",
    event_id: null,
    change_type: "methodology_release",
    detail:
      "Initial methodology release. Oil and fuel infrastructure fire emissions using FRP-based and inventory-based methods with Monte Carlo uncertainty propagation (10,000 samples). Confidence labels: confirmed, probable, claimed.",
    occurred_at: "2026-05-23T00:00:00Z",
  },
];

export default function ChangelogPage() {
  const [filter, setFilter] = useState<FilterType>("all");

  const changelog = useQuery({
    queryKey: ["changelog"],
    queryFn: fetchChangelog,
  });

  const entries = useMemo(() => {
    const raw = changelog.data?.entries ?? FALLBACK_ENTRIES;
    if (filter === "all") return raw;
    return raw.filter((e) => e.change_type === filter);
  }, [changelog.data, filter]);

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Changelog</h1>
        <p className="mt-2 text-sm text-slate-400">
          Public record of methodology versions, event retractions, and
          editorial decisions. Every change is permanent and linked.
        </p>
      </div>

      {/* Filters */}
      <div className="mb-8 flex flex-wrap gap-2">
        {FILTER_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            onClick={() => setFilter(opt.value)}
            className={`rounded-full border px-3.5 py-1.5 text-xs font-medium transition-all ${
              filter === opt.value
                ? "border-orange-500/30 bg-orange-500/10 text-orange-400"
                : "border-white/[0.06] bg-white/[0.02] text-slate-500 hover:border-white/[0.12] hover:text-slate-300"
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* Timeline */}
      {changelog.isLoading && (
        <div className="flex items-center gap-3 py-8">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
          <p className="text-sm text-slate-500">Loading changelog...</p>
        </div>
      )}

      {changelog.error && !changelog.data && (
        <div className="border-l-2 border-white/[0.06] pl-6">
          {FALLBACK_ENTRIES.filter(
            (e) => filter === "all" || e.change_type === filter
          ).map((entry, i) => (
            <ChangelogEntry key={i} entry={entry} index={i} />
          ))}
        </div>
      )}

      {entries.length === 0 && !changelog.isLoading && (
        <p className="py-8 text-sm text-slate-600">
          No entries match the selected filter.
        </p>
      )}

      {entries.length > 0 && (
        <div className="border-l-2 border-white/[0.06] pl-6">
          {entries.map((entry, i) => (
            <ChangelogEntry key={entryAnchor(entry, i)} entry={entry} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}

function ChangelogEntry({
  entry,
  index,
}: {
  entry: ChangelogEntryResponse;
  index: number;
}) {
  const anchor = entryAnchor(entry, index);

  return (
    <div id={anchor} className="relative mb-10 last:mb-0">
      <div className="absolute -left-[31px] top-1 h-3 w-3 rounded-full border-2 border-slate-700 bg-slate-900" />

      <div className="flex flex-wrap items-center gap-2 mb-2">
        <time className="text-xs font-medium text-slate-500 tabular-nums">
          {formatDate(entry.occurred_at)}
        </time>
        <span
          className={`inline-block rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${entryTypeBadgeClass(entry.change_type)}`}
        >
          {entryTypeLabel(entry.change_type)}
        </span>
        <a
          href={`#${anchor}`}
          className="ml-auto text-[10px] text-slate-700 hover:text-slate-400"
          title="Permanent link"
        >
          #
        </a>
      </div>

      {entry.version_id && (
        <p className="text-sm font-semibold text-slate-200">
          {entry.version_id}
        </p>
      )}

      {entry.event_id && (
        <p className="text-xs font-mono text-slate-600">
          Event: {entry.event_id}
        </p>
      )}

      <p className="mt-1.5 text-sm leading-relaxed text-slate-400">
        {entry.detail}
      </p>

      {entry.version_id && (
        <a
          href={`https://github.com/shreyabaid007/conflict-emission-tracker/blob/main/methodology/${entry.version_id}.pdf`}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-3 inline-block text-xs text-orange-400/70 underline underline-offset-2 hover:text-orange-300"
        >
          View methodology PDF
        </a>
      )}

      {entry.event_id && (
        <a
          href={`/event/${entry.event_id}`}
          className="mt-3 inline-block text-xs text-orange-400/70 underline underline-offset-2 hover:text-orange-300"
        >
          View event details
        </a>
      )}
    </div>
  );
}
