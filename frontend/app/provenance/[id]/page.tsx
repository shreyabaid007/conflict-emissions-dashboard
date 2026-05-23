"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { fetchProvenance } from "@/lib/api";
import type { ProvenanceNode } from "@/lib/api";

function nodeTitle(node: ProvenanceNode): string {
  if (node.node_type === "source") {
    return String(node.detail.identifier ?? node.id);
  }
  return String(node.detail.produced_by ?? node.id);
}

function nodeSubtitle(node: ProvenanceNode): string {
  if (node.node_type === "source") {
    const type = String(node.detail.source_type ?? "source");
    const retrieved = node.detail.retrieved_at
      ? ` · retrieved ${new Date(String(node.detail.retrieved_at)).toLocaleDateString()}`
      : "";
    return `${type}${retrieved}`;
  }
  const method = String(node.detail.method ?? "");
  const conf = node.detail.confidence_label
    ? ` · ${node.detail.confidence_label}`
    : "";
  return `${method}${conf}`;
}

/**
 * Standalone provenance view (gap C.8). Every number on the dashboard links
 * here via /provenance/{id} so the full source chain is one click away.
 */
export default function ProvenancePage() {
  const params = useParams();
  const id = String(params.id);

  const provenanceQ = useQuery({
    queryKey: ["provenance", id],
    queryFn: () => fetchProvenance(id),
  });

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Provenance chain</h1>
        <p className="mt-2 text-sm text-slate-400">
          Every emission number traces back through a chain of cited sources.
          No estimate exists without its provenance record.
        </p>
        <p className="mt-2 font-mono text-xs text-slate-600">id: {id}</p>
      </div>

      {provenanceQ.isLoading && (
        <div className="flex items-center gap-3 py-8">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
          <p className="text-sm text-slate-500">Loading provenance…</p>
        </div>
      )}

      {provenanceQ.error && (
        <p className="rounded-lg border border-red-500/20 bg-red-500/5 px-4 py-3 text-sm text-red-400">
          No provenance record found for this id.
        </p>
      )}

      {provenanceQ.data && (
        <ol className="border-l border-white/[0.08] pl-4">
          {provenanceQ.data.chain.map((node) => (
            <li key={node.id} className="relative mb-5 last:mb-0">
              <div className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-slate-700 bg-slate-900" />
              <div className="flex items-center gap-2">
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                    node.node_type === "source"
                      ? "bg-blue-500/10 text-blue-400"
                      : "bg-orange-500/10 text-orange-400"
                  }`}
                >
                  {node.node_type}
                </span>
                <p className="text-sm font-medium text-slate-200">
                  {nodeTitle(node)}
                </p>
              </div>
              <p className="mt-0.5 text-xs text-slate-500">{nodeSubtitle(node)}</p>
              <p className="mt-0.5 font-mono text-[10px] text-slate-700">
                {node.id}
              </p>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
