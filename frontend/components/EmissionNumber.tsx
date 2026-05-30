import Link from "next/link";
import { ConfidenceBadge } from "@/components/ConfidenceBadge";
import { UncertaintyBar } from "@/components/UncertaintyBar";

interface EmissionNumberProps {
  /** Median and bounds in tCO₂e. */
  p5: number;
  p50: number;
  p95: number;
  unit?: string;
  /** Confidence label (CONFIRMED / VERIFIED / REPORTED …). */
  confidence: string;
  /** ISO timestamp of the last update to this estimate. */
  lastUpdated: string;
  /** Provenance-record id for the click-through audit link. */
  provenanceId: string;
  label?: string;
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  });
}

/**
 * Canonical rendering for any single emission number on the dashboard.
 *
 * Every published number is shown as a distribution (p5/p50/p95) — never a
 * point — with its confidence badge, a last-updated timestamp, and a
 * click-through link to its full provenance chain (CLAUDE.md core principles
 * #1 Provenance and #2 Uncertainty; v2 §6 click-through auditability).
 */
export function EmissionNumber({
  p5,
  p50,
  p95,
  unit = "tCO₂e",
  confidence,
  lastUpdated,
  provenanceId,
  label,
}: EmissionNumberProps) {
  return (
    <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        {label ? (
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
            {label}
          </span>
        ) : (
          <span />
        )}
        <ConfidenceBadge label={confidence} />
      </div>

      <UncertaintyBar p5={p5} p50={p50} p95={p95} unit={unit} />

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-600">
        <span>
          Last updated{" "}
          <time dateTime={lastUpdated} className="tabular-nums text-slate-500">
            {formatTimestamp(lastUpdated)}
          </time>
        </span>
        <Link
          href={`/provenance/${provenanceId}`}
          className="text-orange-400/70 underline underline-offset-2 hover:text-orange-300"
        >
          View provenance →
        </Link>
      </div>
    </div>
  );
}
