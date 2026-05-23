"use client";

import { useEffect, useState } from "react";

const STORAGE_KEY = "wced-disclaimer-accepted";

export function DisclaimerModal() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    try {
      const accepted = localStorage.getItem(STORAGE_KEY);
      if (!accepted) {
        setVisible(true);
      }
    } catch {
      setVisible(true);
    }
  }, []);

  function accept() {
    try {
      localStorage.setItem(STORAGE_KEY, "true");
    } catch {}
    setVisible(false);
  }

  if (!visible) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="mx-4 max-w-lg rounded-2xl border border-white/[0.08] bg-slate-900 p-8 shadow-2xl">
        <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-xl bg-orange-500/10">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
            <path
              d="M10 2L2 18h16L10 2z"
              stroke="#f97316"
              strokeWidth="1.5"
              fill="none"
            />
            <line x1="10" y1="8" x2="10" y2="12" stroke="#f97316" strokeWidth="1.5" strokeLinecap="round" />
            <circle cx="10" cy="15" r="0.75" fill="#f97316" />
          </svg>
        </div>
        <h2 className="mb-3 text-lg font-semibold text-white">
          Methodology Disclaimer
        </h2>
        <div className="mb-6 space-y-3 text-sm leading-relaxed text-slate-400">
          <p>
            This dashboard presents{" "}
            <strong className="text-slate-200">estimated CO&#8322; emissions</strong> from oil and fuel
            infrastructure fires using public satellite data and peer-reviewed
            emission factors.
          </p>
          <p>
            All estimates are reported as distributions (5th/50th/95th
            percentiles), not point values. Uncertainty bounds are wide by
            design.
          </p>
          <p>
            Data is updated daily from NASA FIRMS, ESA Sentinel, and ACLED.
            This is <strong className="text-slate-200">not</strong> a real-time system. There is
            hours-to-days latency between an event and its appearance here.
          </p>
          <p>
            Read the full{" "}
            <a href="/methodology" className="text-orange-400 underline underline-offset-2 hover:text-orange-300">
              methodology document
            </a>{" "}
            before citing any figures from this dashboard.
          </p>
        </div>
        <button
          onClick={accept}
          className="w-full rounded-xl bg-orange-500 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-orange-400"
        >
          I understand — continue
        </button>
      </div>
    </div>
  );
}
