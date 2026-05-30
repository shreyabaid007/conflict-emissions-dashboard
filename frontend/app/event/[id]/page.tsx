"use client";

import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import Link from "next/link";
import {
  fetchEventDetail,
  fetchEventProvenance,
  fetchEventAssessment,
  fetchFacility,
  fetchEventAcled,
} from "@/lib/api";
import type {
  EmissionEstimate,
  ProvenanceNode,
  AcledEvent,
  DamageAssessmentData,
} from "@/lib/api";
import { UncertaintyBar } from "@/components/UncertaintyBar";
import { formatCO2, formatCO2Compact } from "@/lib/format";
import { CONFIDENCE_COLORS, CONFIDENCE_LABELS } from "@/lib/constants";
import {
  ReactCompareSlider,
  ReactCompareSliderImage,
} from "react-compare-slider";

const GITHUB_REPO_URL =
  "https://github.com/shreyabaid007/war-emission-tracker";

function parseWktPoint(wkt: string): { lat: number; lon: number } | null {
  const match = wkt.match(/POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)/i);
  if (!match) return null;
  return { lon: parseFloat(match[1]), lat: parseFloat(match[2]) };
}

function ConfidenceBadge({ label }: { label: string }) {
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

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    PUBLISHED: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
    PENDING_REVIEW: "bg-amber-500/10 text-amber-400 border-amber-500/20",
    RETRACTED: "bg-red-500/10 text-red-400 border-red-500/20",
  };
  const cls = styles[status] ?? "bg-white/5 text-slate-400 border-white/10";
  return (
    <span className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${cls}`}>
      {status.toLowerCase().replace("_", " ")}
    </span>
  );
}

function Section({
  title,
  children,
  id,
}: {
  title: string;
  children: React.ReactNode;
  id?: string;
}) {
  return (
    <section id={id} className="glass-card p-6">
      <h2 className="mb-4 text-[11px] font-semibold uppercase tracking-widest text-slate-500">
        {title}
      </h2>
      {children}
    </section>
  );
}

function MethodBar({
  label,
  p5,
  p50,
  p95,
  maxVal,
}: {
  label: string;
  p5: number;
  p50: number;
  p95: number;
  maxVal: number;
}) {
  const barWidth = maxVal > 0 ? (p50 / maxVal) * 100 : 0;
  const lo = maxVal > 0 ? (p5 / maxVal) * 100 : 0;
  const hi = maxVal > 0 ? (p95 / maxVal) * 100 : 0;
  return (
    <div className="mb-4 last:mb-0">
      <div className="mb-1.5 flex items-baseline justify-between text-sm">
        <span className="font-medium text-slate-300">{label}</span>
        <span className="tabular-nums text-orange-400">
          {formatCO2Compact(p50)}
        </span>
      </div>
      <div className="relative h-5 w-full overflow-hidden rounded-full bg-white/[0.04]">
        <div
          className="absolute inset-y-0 rounded-full bg-orange-500/15"
          style={{ left: `${lo}%`, width: `${Math.max(hi - lo, 0.5)}%` }}
        />
        <div
          className="absolute inset-y-0 w-1 rounded-full bg-orange-400"
          style={{ left: `${barWidth}%` }}
        />
      </div>
      <div className="mt-1 flex justify-between text-xs text-slate-600">
        <span>{formatCO2Compact(p5)}</span>
        <span>{formatCO2Compact(p95)}</span>
      </div>
    </div>
  );
}

function MethodDisagreementWarning({
  estimates,
}: {
  estimates: EmissionEstimate[];
}) {
  if (estimates.length < 2) return null;
  const p50s = estimates.map((e) => e.p50).filter((v) => v > 0);
  if (p50s.length < 2) return null;
  const max = Math.max(...p50s);
  const min = Math.min(...p50s);
  if (min > 0 && max / min > 2) {
    return (
      <div className="mt-4 flex items-start gap-2.5 rounded-lg border border-amber-500/20 bg-amber-500/5 p-4 text-sm text-amber-300">
        <span className="mt-0.5 text-base">&#9888;</span>
        <div>
          <p className="font-medium">Methods disagree by &gt;2x</p>
          <p className="mt-1 text-xs text-amber-400/70">
            The highest estimate ({formatCO2Compact(max)}) is{" "}
            {(max / min).toFixed(1)}x the lowest ({formatCO2Compact(min)}).
            This may indicate differing assumptions about fuel type, burn
            duration, or fraction destroyed. See methodology for reconciliation
            procedure.
          </p>
        </div>
      </div>
    );
  }
  return null;
}

function FrpTimeline({
  detectedAt,
  lastSeenAt,
  peakFrp,
  totalIntegral,
}: {
  detectedAt: string;
  lastSeenAt: string;
  peakFrp: number;
  totalIntegral: number | null;
}) {
  const start = new Date(detectedAt);
  const end = new Date(lastSeenAt);
  const durationHours = Math.max(
    (end.getTime() - start.getTime()) / (1000 * 60 * 60),
    1
  );

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-4 text-center">
        {[
          { label: "Peak FRP", value: `${peakFrp.toFixed(0)}`, unit: "MW" },
          {
            label: "Duration",
            value: durationHours < 24 ? `${durationHours.toFixed(1)}` : `${(durationHours / 24).toFixed(1)}`,
            unit: durationHours < 24 ? "h" : "d",
          },
          {
            label: "FRP integral",
            value: totalIntegral != null ? `${(totalIntegral / 1000).toFixed(1)}` : "—",
            unit: totalIntegral != null ? "GJ" : "",
          },
        ].map((item) => (
          <div key={item.label} className="rounded-lg bg-white/[0.03] border border-white/[0.06] p-4">
            <p className="text-[11px] uppercase tracking-wider text-slate-500">{item.label}</p>
            <p className="mt-1 text-xl font-bold tabular-nums text-slate-200">
              {item.value}
              {item.unit && <span className="ml-1 text-sm font-normal text-slate-500">{item.unit}</span>}
            </p>
          </div>
        ))}
      </div>
      <div className="rounded-lg bg-white/[0.03] border border-white/[0.06] p-4">
        <div className="flex h-24 items-end gap-px">
          {Array.from({ length: 20 }).map((_, i) => {
            const frac = i / 19;
            const height = Math.max(
              5,
              Math.sin(frac * Math.PI) * 80 * (0.7 + Math.random() * 0.3)
            );
            return (
              <div
                key={i}
                className="flex-1 rounded-t bg-gradient-to-t from-orange-500/40 to-orange-400/80"
                style={{ height: `${height}%` }}
              />
            );
          })}
        </div>
        <div className="mt-3 flex justify-between text-xs text-slate-500">
          <span>{start.toLocaleDateString()}</span>
          <span>{end.toLocaleDateString()}</span>
        </div>
      </div>
      <p className="text-xs text-slate-600 italic">
        Illustrative FRP profile based on peak and duration. Per-overpass FRP
        values available via API.
      </p>
    </div>
  );
}

function SentinelSlider({ eventId }: { eventId: string }) {
  const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  const beforeUrl = `${API_BASE}/api/v1/events/${eventId}/s2/before`;
  const afterUrl = `${API_BASE}/api/v1/events/${eventId}/s2/after`;
  const [hasError, setHasError] = useState(false);

  if (hasError) {
    return (
      <div className="flex h-64 items-center justify-center rounded-lg bg-white/[0.02] border border-white/[0.06]">
        <p className="text-sm text-slate-600">
          No Sentinel-2 imagery available for this event.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border border-white/[0.06]">
      <ReactCompareSlider
        itemOne={
          <ReactCompareSliderImage
            src={beforeUrl}
            alt="Pre-event Sentinel-2"
            onError={() => setHasError(true)}
          />
        }
        itemTwo={
          <ReactCompareSliderImage
            src={afterUrl}
            alt="Post-event Sentinel-2"
            onError={() => setHasError(true)}
          />
        }
        style={{ height: 320 }}
      />
      <div className="flex justify-between px-4 py-2 text-xs text-slate-500 bg-white/[0.02]">
        <span>Before</span>
        <span>After</span>
      </div>
    </div>
  );
}

function ProvenanceTree({ chain }: { chain: ProvenanceNode[] }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (nodeId: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });

  if (chain.length === 0) {
    return (
      <p className="text-sm text-slate-600">No provenance data available.</p>
    );
  }

  return (
    <div className="space-y-2">
      {chain.map((node) => {
        const isOpen = expanded.has(node.id);
        const detail = node.detail;
        const label =
          node.node_type === "source"
            ? `[${detail.source_type}] ${detail.identifier}`
            : `[${detail.method}] ${detail.produced_by}`;
        const sourceUrl =
          node.node_type === "source" &&
          typeof detail.identifier === "string" &&
          detail.identifier.startsWith("http")
            ? detail.identifier
            : null;

        return (
          <div
            key={node.id}
            className="rounded-lg border border-white/[0.06] bg-white/[0.02] text-sm"
          >
            <button
              onClick={() => toggle(node.id)}
              className="flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-white/[0.03]"
            >
              <span className="text-xs text-slate-600">
                {isOpen ? "▼" : "▶"}
              </span>
              <span
                className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                  node.node_type === "source"
                    ? "bg-blue-500/10 text-blue-400 border border-blue-500/20"
                    : "bg-white/5 text-slate-400 border border-white/10"
                }`}
              >
                {node.node_type}
              </span>
              <span className="flex-1 truncate font-medium text-slate-300">
                {label}
              </span>
              {sourceUrl && (
                <a
                  href={sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="shrink-0 text-xs text-orange-400 underline underline-offset-2"
                  onClick={(e) => e.stopPropagation()}
                >
                  open source
                </a>
              )}
            </button>
            {isOpen && (
              <div className="border-t border-white/[0.06] px-4 py-3">
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs">
                  {Object.entries(detail).map(([k, v]) => (
                    <Fragment key={k}>
                      <dt className="font-medium text-slate-500">{k}</dt>
                      <dd className="break-all text-slate-400">
                        {typeof v === "string" && v.startsWith("http") ? (
                          <a
                            href={v}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-orange-400 underline underline-offset-2"
                          >
                            {v}
                          </a>
                        ) : (
                          String(v ?? "—")
                        )}
                      </dd>
                    </Fragment>
                  ))}
                </dl>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function AcledCorroboration({ events }: { events: AcledEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-slate-600">No linked ACLED events.</p>;
  }
  return (
    <ul className="divide-y divide-white/[0.04]">
      {events.map((evt) => (
        <li key={evt.id} className="py-3 first:pt-0 last:pb-0">
          <div className="flex items-baseline justify-between">
            <span className="text-sm font-medium text-slate-200">{evt.event_type}</span>
            <span className="text-xs text-slate-500">{evt.event_date}</span>
          </div>
          {evt.sub_event_type && (
            <p className="text-xs text-slate-500">{evt.sub_event_type}</p>
          )}
          <p className="mt-1 text-xs text-slate-400">
            {evt.location ?? evt.admin1 ?? evt.country} &mdash;{" "}
            {evt.latitude.toFixed(4)}, {evt.longitude.toFixed(4)}
          </p>
          {evt.source && (
            <p className="mt-1 text-xs text-slate-600">
              Source: {evt.source}
            </p>
          )}
          {evt.notes && (
            <p className="mt-1 text-xs text-slate-500 line-clamp-3">
              {evt.notes}
            </p>
          )}
        </li>
      ))}
    </ul>
  );
}

function DamageAssessmentSection({ data }: { data: DamageAssessmentData }) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-4">
        {(
          [
            ["Low", data.fraction_destroyed_low],
            ["Mode", data.fraction_destroyed_mode],
            ["High", data.fraction_destroyed_high],
          ] as const
        ).map(([label, val]) => (
          <div key={label} className="rounded-lg bg-white/[0.03] border border-white/[0.06] p-4 text-center">
            <p className="text-[11px] uppercase tracking-wider text-slate-500">{label}</p>
            <p className="mt-1 text-xl font-bold tabular-nums text-slate-200">
              {(val * 100).toFixed(0)}%
            </p>
          </div>
        ))}
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
        <dt className="text-slate-500">Assessed by</dt>
        <dd className="text-slate-300">{data.assessed_by}</dd>
        <dt className="text-slate-500">Method</dt>
        <dd className="text-slate-300">{data.assessment_method}</dd>
        <dt className="text-slate-500">Date</dt>
        <dd className="text-slate-300">{new Date(data.assessed_at).toLocaleDateString()}</dd>
        {data.notes && (
          <>
            <dt className="text-slate-500">Notes</dt>
            <dd className="text-slate-400">{data.notes}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

export default function EventDetailPage() {
  const { id } = useParams<{ id: string }>();

  const eventQ = useQuery({
    queryKey: ["event-detail", id],
    queryFn: () => fetchEventDetail(id),
    enabled: !!id,
  });

  const provenanceQ = useQuery({
    queryKey: ["event-provenance", id],
    queryFn: () => fetchEventProvenance(id),
    enabled: !!id,
  });

  const assessmentQ = useQuery({
    queryKey: ["event-assessment", id],
    queryFn: () => fetchEventAssessment(id),
    enabled: !!id,
  });

  const facilityId = eventQ.data?.data.facility_id;
  const facilityQ = useQuery({
    queryKey: ["facility", facilityId],
    queryFn: () => fetchFacility(facilityId!),
    enabled: !!facilityId,
  });

  const acledQ = useQuery({
    queryKey: ["event-acled", id],
    queryFn: () => fetchEventAcled(id),
    enabled: !!id,
  });

  if (eventQ.isLoading) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-12">
        <div className="flex items-center gap-3">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
          <p className="text-sm text-slate-500">Loading event...</p>
        </div>
      </div>
    );
  }

  if (eventQ.error || !eventQ.data) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-12">
        <div className="glass-card p-6">
          <p className="text-sm text-red-400">
            Failed to load event.{" "}
            {eventQ.error instanceof Error ? eventQ.error.message : ""}
          </p>
          <Link href="/" className="mt-3 inline-block text-sm text-orange-400 underline underline-offset-2">
            Back to dashboard
          </Link>
        </div>
      </div>
    );
  }

  const event = eventQ.data.data;
  const estimates = eventQ.data.estimates;
  const facility = facilityQ.data?.data;
  const provenance = provenanceQ.data?.chain ?? [];
  const assessment = assessmentQ.data?.data ?? null;
  const acledEvents = acledQ.data ?? [];

  const finalEstimate = estimates[0] ?? null;
  const methodEstimates = estimates.filter(
    (e) => e.methodology_version === finalEstimate?.methodology_version
  );
  const maxP95 = Math.max(...methodEstimates.map((e) => e.p95), 1);

  const coords = facilityQ.data
    ? parseWktPoint(facilityQ.data.geometry_wkt)
    : null;

  const discussUrl = `${GITHUB_REPO_URL}/issues/new?title=${encodeURIComponent(
    `Event ${id}`
  )}&body=${encodeURIComponent(
    `Discussion about event \`${id}\`\n\nEvent page: ${
      typeof window !== "undefined" ? window.location.href : ""
    }\n\n---\n`
  )}`;

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <Link
        href="/"
        className="text-xs text-slate-500 transition-colors hover:text-orange-400"
      >
        &larr; Back to dashboard
      </Link>

      <header className="mt-5">
        <div className="flex flex-wrap items-start gap-3">
          <h1 className="text-2xl font-bold text-white">
            {facility?.name ?? `Facility ${event.facility_id.slice(0, 8)}`}
          </h1>
          <div className="flex gap-2">
            <ConfidenceBadge label={event.confidence_label} />
            <StatusBadge status={event.status} />
          </div>
        </div>
        <p className="mt-2 text-sm text-slate-400">
          {new Date(event.detected_at).toLocaleDateString("en-US", {
            year: "numeric",
            month: "long",
            day: "numeric",
          })}
          {coords && (
            <>
              {" "}
              &middot; {coords.lat.toFixed(4)}&deg;N,{" "}
              {coords.lon.toFixed(4)}&deg;E
            </>
          )}
          {facility?.facility_type && (
            <>
              {" "}
              &middot;{" "}
              {facility.facility_type.toLowerCase().replace(/_/g, " ")}
            </>
          )}
        </p>
        {facility?.operator && (
          <p className="mt-1 text-xs text-slate-600">
            Operator: {facility.operator}
          </p>
        )}
      </header>

      <div className="mt-8 space-y-6">
        {finalEstimate && (
          <Section title="Final emission estimate" id="estimate">
            <div className="mb-1 flex items-center gap-2">
              <span className="inline-flex items-center rounded-full border border-orange-500/20 bg-orange-500/10 px-2.5 py-0.5 text-xs font-medium text-orange-400">
                Methodology v{finalEstimate.methodology_version}
              </span>
              <span className="text-xs text-slate-600">
                {finalEstimate.method.replace(/_/g, " ")}
              </span>
            </div>
            <div className="mb-5 mt-4">
              <p className="text-4xl font-bold tabular-nums text-orange-400 stat-glow">
                {formatCO2(finalEstimate.p50).value}{" "}
                <span className="text-lg font-normal text-orange-400/60">
                  {formatCO2(finalEstimate.p50).unit}
                </span>
              </p>
              <p className="mt-2 text-sm text-slate-500">
                90% CI: {formatCO2Compact(finalEstimate.p5)} &ndash;{" "}
                {formatCO2Compact(finalEstimate.p95)}
              </p>
            </div>
            <UncertaintyBar
              p5={finalEstimate.p5}
              p50={finalEstimate.p50}
              p95={finalEstimate.p95}
            />
            <p className="mt-3 text-xs text-slate-600">
              Computed{" "}
              {new Date(finalEstimate.created_at).toLocaleDateString()}
            </p>
          </Section>
        )}

        {methodEstimates.length > 1 && (
          <Section title="Estimation methods comparison" id="methods">
            {methodEstimates.map((est) => (
              <MethodBar
                key={est.id}
                label={est.method.replace(/_/g, " ")}
                p5={est.p5}
                p50={est.p50}
                p95={est.p95}
                maxVal={maxP95}
              />
            ))}
            {(() => {
              const p50s = methodEstimates
                .map((e) => e.p50)
                .filter((v) => v > 0);
              if (p50s.length >= 2) {
                const ratio = Math.max(...p50s) / Math.min(...p50s);
                return (
                  <div className="mt-4 rounded-lg bg-white/[0.03] border border-white/[0.06] p-4 text-sm">
                    <span className="font-medium text-slate-300">
                      Reconciliation ratio:{" "}
                    </span>
                    <span className="tabular-nums text-orange-400">
                      {ratio.toFixed(2)}x
                    </span>
                    <span className="ml-2 text-xs text-slate-600">
                      (max / min of method p50 estimates)
                    </span>
                  </div>
                );
              }
              return null;
            })()}
            <MethodDisagreementWarning estimates={methodEstimates} />
          </Section>
        )}

        <Section title="Fire Radiative Power timeline" id="frp">
          <FrpTimeline
            detectedAt={event.detected_at}
            lastSeenAt={event.last_seen_at}
            peakFrp={event.peak_frp_mw}
            totalIntegral={event.total_frp_integral_mj}
          />
        </Section>

        <Section title="Sentinel-2 before / after" id="sentinel">
          <SentinelSlider eventId={id} />
        </Section>

        <Section title="Provenance chain" id="provenance">
          {provenanceQ.isLoading ? (
            <div className="flex items-center gap-2 py-4">
              <div className="h-3 w-3 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
              <p className="text-sm text-slate-500">Loading provenance...</p>
            </div>
          ) : (
            <ProvenanceTree chain={provenance} />
          )}
        </Section>

        <Section title="ACLED corroboration" id="acled">
          {acledQ.isLoading ? (
            <div className="flex items-center gap-2 py-4">
              <div className="h-3 w-3 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
              <p className="text-sm text-slate-500">Loading ACLED events...</p>
            </div>
          ) : (
            <AcledCorroboration events={acledEvents} />
          )}
        </Section>

        <Section title="Damage assessment" id="damage">
          {assessmentQ.isLoading ? (
            <div className="flex items-center gap-2 py-4">
              <div className="h-3 w-3 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
              <p className="text-sm text-slate-500">Loading assessment...</p>
            </div>
          ) : assessment ? (
            <DamageAssessmentSection data={assessment} />
          ) : (
            <p className="text-sm text-slate-600">
              No damage assessment available for this event.
            </p>
          )}
        </Section>

        <div className="flex flex-wrap items-center justify-between gap-4 glass-card p-6">
          <div>
            <h2 className="text-[11px] font-semibold uppercase tracking-widest text-slate-500">
              Questions or corrections?
            </h2>
            <p className="mt-1 text-sm text-slate-400">
              Open a GitHub issue pre-filled with this event ID.
            </p>
          </div>
          <a
            href={discussUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="shrink-0 rounded-xl bg-orange-500 px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-orange-400"
          >
            Discuss on GitHub
          </a>
        </div>
      </div>

      <footer className="mt-10 border-t border-white/[0.04] pt-5 text-xs text-slate-600">
        <p>
          Event ID: <code className="font-mono text-slate-500">{id}</code> &middot; Detection
          source: {event.detection_source} &middot; Data license:{" "}
          {eventQ.data.data_license}
        </p>
        <p className="mt-1">{eventQ.data.attribution}</p>
      </footer>
    </div>
  );
}
