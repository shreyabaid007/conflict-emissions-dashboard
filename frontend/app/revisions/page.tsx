"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { fetchRevisions, type RevisionEntry } from "@/lib/api";

type FilterType = "all" | "retractions" | "under_review" | "publications";

const FILTER_OPTIONS: { value: FilterType; label: string }[] = [
  { value: "all", label: "All" },
  { value: "retractions", label: "Retractions" },
  { value: "under_review", label: "Under review" },
  { value: "publications", label: "Publications" },
];

function matchesFilter(entry: RevisionEntry, filter: FilterType): boolean {
  switch (filter) {
    case "all":
      return true;
    case "retractions":
      return entry.action === "retract";
    case "under_review":
      return entry.public_note === "under review";
    case "publications":
      return entry.action === "approve";
    default:
      return true;
  }
}

function actionLabel(entry: RevisionEntry): string {
  switch (entry.action) {
    case "approve":
      return "Published";
    case "reject":
      return "Rejected";
    case "resubmit":
      return "Resubmitted";
    case "retract":
      return "Retracted";
    case "anomaly_retract":
      return "Auto-retracted (anomaly)";
    case "recompute_route_to_review":
      return "Returned to review (recompute)";
    default:
      return entry.action;
  }
}

function actionBadgeClass(entry: RevisionEntry): string {
  switch (entry.action) {
    case "approve":
      return "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
    case "retract":
      return "bg-red-500/10 text-red-400 border-red-500/20";
    case "anomaly_retract":
      return "bg-amber-500/10 text-amber-400 border-amber-500/20";
    case "reject":
      return "bg-red-500/10 text-red-400 border-red-500/20";
    default:
      return "bg-white/5 text-slate-400 border-white/10";
  }
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  });
}

/**
 * Public revision log (gap 1.5). Sourced from the append-only publication_log:
 * retractions and restatements are shown, never silently deleted (CLAUDE.md
 * §"Editorial Workflow"). Anomaly auto-retractions surface an "under review"
 * flag (CLAUDE.md gate #5).
 */
export default function RevisionsPage() {
  const [filter, setFilter] = useState<FilterType>("all");

  const revisions = useQuery({
    queryKey: ["revisions"],
    queryFn: () => fetchRevisions(),
  });

  const entries = useMemo(() => {
    const raw = revisions.data?.data ?? [];
    return raw.filter((e) => matchesFilter(e, filter));
  }, [revisions.data, filter]);

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Revision log</h1>
        <p className="mt-2 text-sm text-slate-400">
          Every publish, retraction, and restatement of a number on this
          dashboard. Corrections are permanent and public — nothing is ever
          silently deleted.
        </p>
      </div>

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

      {revisions.isLoading && (
        <div className="flex items-center gap-3 py-8">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
          <p className="text-sm text-slate-500">Loading revision log…</p>
        </div>
      )}

      {revisions.error && (
        <p className="rounded-lg border border-red-500/20 bg-red-500/5 px-4 py-3 text-sm text-red-400">
          Could not load the revision log.
        </p>
      )}

      {!revisions.isLoading && entries.length === 0 && (
        <p className="py-8 text-sm text-slate-600">
          No revisions match the selected filter.
        </p>
      )}

      {entries.length > 0 && (
        <div className="border-l-2 border-white/[0.06] pl-6">
          {entries.map((entry) => (
            <RevisionRow key={entry.id} entry={entry} />
          ))}
        </div>
      )}
    </div>
  );
}

function RevisionRow({ entry }: { entry: RevisionEntry }) {
  return (
    <div className="relative mb-10 last:mb-0">
      <div className="absolute -left-[31px] top-1 h-3 w-3 rounded-full border-2 border-slate-700 bg-slate-900" />

      <div className="mb-2 flex flex-wrap items-center gap-2">
        <time className="text-xs font-medium tabular-nums text-slate-500">
          {formatDate(entry.created_at)}
        </time>
        <span
          className={`inline-block rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${actionBadgeClass(entry)}`}
        >
          {actionLabel(entry)}
        </span>
        {entry.public_note && (
          <span className="inline-block rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-400">
            {entry.public_note}
          </span>
        )}
      </div>

      <p className="text-xs text-slate-500">
        {entry.from_state.toLowerCase().replace("_", " ")} →{" "}
        <span className="text-slate-300">
          {entry.to_state.toLowerCase().replace("_", " ")}
        </span>
        <span className="text-slate-600"> · by {entry.actor}</span>
      </p>

      {entry.reason && (
        <p className="mt-1.5 text-sm leading-relaxed text-slate-400">
          {entry.reason}
        </p>
      )}

      <Link
        href={`/event/${entry.target_id}`}
        className="mt-3 inline-block text-xs text-orange-400/70 underline underline-offset-2 hover:text-orange-300"
      >
        View event details
      </Link>
    </div>
  );
}
