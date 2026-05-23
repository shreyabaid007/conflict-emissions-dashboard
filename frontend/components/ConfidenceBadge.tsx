import { CONFIDENCE_COLORS, CONFIDENCE_LABELS } from "@/lib/constants";

/**
 * Confidence badge shared across event, number, and revision views so every
 * surfaced number carries the same confidence semantics (CLAUDE.md core
 * principle #4 — triangulation before publication).
 */
export function ConfidenceBadge({ label }: { label: string }) {
  const color = CONFIDENCE_COLORS[label] ?? "#a3a3a3";
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium"
      style={{ borderColor: `${color}40`, color, backgroundColor: `${color}15` }}
      title={CONFIDENCE_LABELS[label]}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {label}
    </span>
  );
}
