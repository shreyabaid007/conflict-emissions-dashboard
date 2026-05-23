"use client";

import type { ReactNode } from "react";
import { Tooltip } from "@/components/Tooltip";

interface HeadlineCardProps {
  label: string;
  tooltip: ReactNode;
  children: ReactNode;
  accent?: "orange" | "blue" | "emerald" | "slate";
}

const ACCENT_STYLES = {
  orange: "border-orange-500/10 bg-gradient-to-br from-orange-500/[0.06] to-transparent",
  blue: "border-blue-500/10 bg-gradient-to-br from-blue-500/[0.06] to-transparent",
  emerald: "border-emerald-500/10 bg-gradient-to-br from-emerald-500/[0.06] to-transparent",
  slate: "border-white/[0.06] bg-white/[0.02]",
};

export function HeadlineCard({
  label,
  tooltip,
  children,
  accent = "slate",
}: HeadlineCardProps) {
  return (
    <div
      className={`rounded-xl border p-5 transition-all duration-200 hover:border-white/[0.12] ${ACCENT_STYLES[accent]}`}
    >
      <Tooltip content={tooltip}>
        <span className="text-[11px] font-medium uppercase tracking-widest text-slate-500">
          {label}
        </span>
      </Tooltip>
      <div className="mt-3">{children}</div>
    </div>
  );
}
