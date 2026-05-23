"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchMeta } from "@/lib/api";

export function MethodologyBanner() {
  const { data } = useQuery({
    queryKey: ["meta"],
    queryFn: fetchMeta,
  });

  const lastUpdate = data?.last_data_update
    ? new Date(data.last_data_update).toLocaleString("en-US", {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : null;

  return (
    <div className="border-b border-white/[0.04] bg-orange-500/[0.03] px-4 py-1.5 text-center text-xs text-orange-300/70">
      <span className="inline-flex items-center gap-2">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse-slow" />
        Methodology v{data?.methodology_version ?? "1.0.5"}
        {lastUpdate && (
          <>
            <span className="text-slate-600">|</span>
            Last updated: {lastUpdate}
          </>
        )}
      </span>
    </div>
  );
}
