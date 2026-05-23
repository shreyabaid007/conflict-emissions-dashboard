"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchMethodology } from "@/lib/api";

const TOC_SECTIONS = [
  { id: "introduction", label: "1. Introduction", page: 1 },
  { id: "data-model", label: "2. Data Model", page: 2 },
  { id: "fire-detection", label: "2.1 Fire Detection", page: 2 },
  { id: "facility-matching", label: "2.2 Facility Matching", page: 3 },
  { id: "emission-calculation", label: "3. Emission Calculations", page: 4 },
  { id: "frp-method", label: "3.3 FRP Method", page: 5 },
  { id: "inventory-method", label: "3.4 Inventory Method", page: 6 },
  { id: "reconciliation", label: "3.5 Reconciliation", page: 7 },
  { id: "verification", label: "4. Verification", page: 8 },
  { id: "confidence-labels", label: "4.3 Confidence Labels", page: 9 },
  { id: "uncertainty", label: "5. Uncertainty Quantification", page: 10 },
  { id: "references", label: "References", page: 11 },
];

const GITHUB_REPO = "https://github.com/shreyabaid007/war-emission-tracker";

export default function MethodologyPage() {
  const [activeSection, setActiveSection] = useState<string>("introduction");

  const methodology = useQuery({
    queryKey: ["methodology"],
    queryFn: fetchMethodology,
  });

  const version = methodology.data?.version_id ?? "v1.0.5";
  const localPdfUrl = `/methodology/${version}.pdf`;
  const rawPdfUrl = localPdfUrl;

  function handleSectionClick(sectionId: string, page: number) {
    setActiveSection(sectionId);
    const iframe = document.querySelector<HTMLIFrameElement>("#pdf-viewer");
    if (iframe) {
      iframe.src = `${rawPdfUrl}#page=${page}`;
    }
  }

  return (
    <div className="mx-auto max-w-[1400px] px-6 py-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Methodology</h1>
        <p className="mt-2 text-sm text-slate-400">
          {methodology.data
            ? `Version ${methodology.data.version_id} — Released ${new Date(methodology.data.released_at).toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" })}`
            : "Version 1.0.5 — Oil and fuel infrastructure fire emissions"}
        </p>
      </div>

      <div className="flex gap-6 lg:flex-row flex-col">
        <nav className="lg:w-56 shrink-0">
          <div className="sticky top-20 glass-card p-4">
            <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-widest text-slate-500">
              Contents
            </h2>
            <ul className="space-y-0.5">
              {TOC_SECTIONS.map((s) => (
                <li key={s.id}>
                  <button
                    onClick={() => handleSectionClick(s.id, s.page)}
                    className={`w-full text-left text-sm px-2.5 py-1.5 rounded-lg transition-colors ${
                      activeSection === s.id
                        ? "bg-orange-500/10 text-orange-400 font-medium"
                        : "text-slate-400 hover:text-white hover:bg-white/[0.04]"
                    } ${s.label.match(/^\d\.\d/) ? "pl-5" : ""}`}
                  >
                    {s.label}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </nav>

        <div className="flex-1 min-w-0">
          <div className="glass-card overflow-hidden">
            <div className="flex items-center justify-between border-b border-white/[0.06] px-5 py-3">
              <span className="text-xs text-slate-500">
                Methodology PDF — {methodology.data?.version_id ?? "v1.0.5"}
              </span>
              <a
                href={rawPdfUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-orange-400/70 hover:text-orange-300 underline underline-offset-2"
              >
                Open in new tab
              </a>
            </div>
            <iframe
              id="pdf-viewer"
              src={rawPdfUrl}
              className="w-full border-0 bg-white"
              style={{ height: "80vh" }}
              title="Methodology PDF"
            />
          </div>

          <div className="mt-6 glass-card p-6">
            <h2 className="mb-4 text-base font-medium text-slate-200">
              Supplementary materials
            </h2>
            <div className="grid gap-4 sm:grid-cols-3">
              <a
                href={`${GITHUB_REPO}/blob/main/methodology/references.bib`}
                target="_blank"
                rel="noopener noreferrer"
                className="group glass-card-hover p-4"
              >
                <p className="text-sm font-medium text-slate-200 group-hover:text-orange-400">
                  BibTeX references
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  references.bib — all cited sources
                </p>
              </a>
              <a
                href={GITHUB_REPO}
                target="_blank"
                rel="noopener noreferrer"
                className="group glass-card-hover p-4"
              >
                <p className="text-sm font-medium text-slate-200 group-hover:text-orange-400">
                  GitHub repository
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  Source code, data, and issue tracker
                </p>
              </a>
              <a
                href={`${GITHUB_REPO}/tree/main/replication`}
                target="_blank"
                rel="noopener noreferrer"
                className="group glass-card-hover p-4"
              >
                <p className="text-sm font-medium text-slate-200 group-hover:text-orange-400">
                  Replication package
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  Scripts to reproduce all estimates
                </p>
              </a>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
