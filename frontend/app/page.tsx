"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchHeadline, fetchTimeseries, fetchEvents } from "@/lib/api";
import { HeadlineCard } from "@/components/HeadlineCard";
import { CumulativeChart } from "@/components/CumulativeChart";
import { EventMap } from "@/components/EventMap";
import { RecentEventsList } from "@/components/RecentEventsList";
import { Tooltip } from "@/components/Tooltip";
import { formatCO2, formatCO2Compact, daysSince } from "@/lib/format";
import { WAR_START, EPA_CO2_PER_CAR_PER_YEAR } from "@/lib/constants";

function equivalentCars(totalTonnes: number): string {
  const cars = Math.round(totalTonnes / EPA_CO2_PER_CAR_PER_YEAR);
  return `~${cars.toLocaleString()}`;
}

export default function DashboardPage() {
  const headline = useQuery({
    queryKey: ["headline"],
    queryFn: fetchHeadline,
  });

  const timeseries = useQuery({
    queryKey: ["timeseries"],
    queryFn: fetchTimeseries,
  });

  const events = useQuery({
    queryKey: ["events"],
    queryFn: fetchEvents,
  });

  const conflictDays = daysSince(WAR_START);

  const recentEvents = useMemo(() => {
    if (!events.data) return [];
    return [...events.data]
      .filter((e) => e.status === "published" && e.emission_p50 != null && e.emission_p50 > 0)
      .sort((a, b) => b.date.localeCompare(a.date))
      .slice(0, 10);
  }, [events.data]);

  const mapEvents = useMemo(() => {
    if (!events.data) return [];
    return events.data.filter((e) => e.status === "published");
  }, [events.data]);

  const eventsWithEstimates = useMemo(() => {
    if (!events.data) return 0;
    return events.data.filter(
      (e) => e.status === "published" && e.emission_p50 != null && e.emission_p50 > 0,
    ).length;
  }, [events.data]);

  const totalEventsDetected = useMemo(() => {
    if (!events.data) return 0;
    return events.data.filter((e) => e.status === "published").length;
  }, [events.data]);

  if (headline.isLoading) {
    return (
      <div className="mx-auto max-w-[1400px] px-6 py-20">
        <div className="flex items-center gap-3">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
          <p className="text-sm text-slate-500">Loading dashboard...</p>
        </div>
      </div>
    );
  }

  if (headline.error) {
    return (
      <div className="mx-auto max-w-[1400px] px-6 py-20">
        <div className="glass-card max-w-md p-6">
          <p className="text-sm text-red-400">
            Failed to load data. Is the API running at{" "}
            <code className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-xs text-orange-400">
              {process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}
            </code>
            ?
          </p>
        </div>
      </div>
    );
  }

  const h = headline.data;
  if (!h) return null;

  const p50 = formatCO2(h.total_p50);
  const p5Fmt = formatCO2Compact(h.total_p5);
  const p95Fmt = formatCO2Compact(h.total_p95);

  return (
    <div className="mx-auto max-w-[1400px] px-6 py-8">
      {/* Hero heading */}
      <div className="mb-10 animate-fade-in">
        <h1 className="text-3xl font-bold tracking-tight text-white">
          War Carbon Emissions Dashboard
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-slate-400">
          Estimated CO&#8322; emissions from oil and fuel infrastructure fires
          during the 2026 Iran–US–Israel conflict.
        </p>
      </div>

      {/* Headline cards */}
      <div className="mb-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="animate-fade-in animate-fade-in-delay-1">
          <HeadlineCard
            label="Cumulative emissions"
            accent="orange"
            tooltip={
              <>
                Sum of median (50th percentile) emission estimates across all
                published fire events. The 90% confidence interval spans the 5th
                to 95th percentile of the Monte Carlo distribution (10,000
                samples per event). Methodology v1.0.5, FRP and inventory methods
                reconciled.
              </>
            }
          >
            <p className="text-3xl font-bold tabular-nums text-orange-400 stat-glow">
              {p50.value}
              <span className="ml-1.5 text-sm font-normal text-orange-400/60">
                {p50.unit}
              </span>
            </p>
            <p className="mt-2 text-xs tabular-nums text-slate-500">
              {p5Fmt} – {p95Fmt} at 90% confidence
            </p>
            <p className="mt-1 text-[11px] text-slate-600">
              Across {eventsWithEstimates} verified events at{" "}
              {h.facility_count} facilities
            </p>
          </HeadlineCard>
        </div>

        <div className="animate-fade-in animate-fade-in-delay-2">
          <HeadlineCard
            label="Days of conflict"
            accent="blue"
            tooltip={
              <>
                Calendar days since the conflict start date of February 28, 2026.
                This count is computed client-side from the fixed start date and
                does not depend on API data.
              </>
            }
          >
            <p className="text-3xl font-bold tabular-nums text-blue-400">
              {conflictDays}
              <span className="ml-1.5 text-sm font-normal text-blue-400/60">
                days
              </span>
            </p>
            <p className="mt-2 text-xs text-slate-500">
              Since Feb 28, 2026
            </p>
          </HeadlineCard>
        </div>

        <div className="animate-fade-in animate-fade-in-delay-3">
          <HeadlineCard
            label="Emission estimates"
            accent="emerald"
            tooltip={
              <>
                Number of published fire events that have completed the full
                emission quantification pipeline (FRP integration, facility
                matching, Monte Carlo simulation). Remaining events are still
                being processed or lack sufficient data for estimation.
              </>
            }
          >
            <p className="text-3xl font-bold tabular-nums text-emerald-400">
              {eventsWithEstimates}
              <span className="mx-1.5 text-sm font-normal text-slate-600">
                of
              </span>
              <span className="text-emerald-400/60">{totalEventsDetected}</span>
              <span className="ml-1.5 text-sm font-normal text-emerald-400/60">
                events
              </span>
            </p>
            <p className="mt-2 text-xs text-slate-500">
              have emission estimates
            </p>
          </HeadlineCard>
        </div>

        <div className="animate-fade-in animate-fade-in-delay-4">
          <HeadlineCard
            label="Equivalent to"
            tooltip={
              <>
                Comparison using EPA average of {EPA_CO2_PER_CAR_PER_YEAR}{" "}
                tCO&#8322;e per passenger car per year (EPA, 2024). This is a
                rough comparator for scale only — methodologies differ.
              </>
            }
          >
            <p className="text-2xl font-bold tabular-nums text-slate-200">
              {equivalentCars(h.total_p50)}
            </p>
            <p className="mt-2 text-xs text-slate-500">
              passenger cars driven for a year
            </p>
            <p className="mt-1 text-[11px] text-slate-600">
              at {EPA_CO2_PER_CAR_PER_YEAR} tCO&#8322;e/car/year (
              <Tooltip
                content={
                  <>
                    EPA average annual emissions per passenger vehicle. Source:
                    EPA Greenhouse Gas Emissions from a Typical Passenger Vehicle
                    (2024).
                  </>
                }
              >
                EPA
              </Tooltip>
              )
            </p>
          </HeadlineCard>
        </div>
      </div>

      {/* Cumulative time series chart */}
      <div className="mb-8 glass-card p-6">
        <div className="mb-4 flex items-baseline justify-between">
          <Tooltip
            content={
              <>
                Daily cumulative CO&#8322; from all published fire events. The
                shaded band shows the 90% confidence interval (5th–95th
                percentile). Red dashed lines mark major events. Data source:
                WCED API, updated daily from NASA FIRMS + facility matching +
                emission quantification pipeline.
              </>
            }
          >
            <h2 className="text-sm font-medium text-slate-300">
              Cumulative emissions over time
            </h2>
          </Tooltip>
        </div>
        {timeseries.isLoading && (
          <div className="flex items-center justify-center py-16">
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
          </div>
        )}
        {timeseries.data && (
          <CumulativeChart
            points={timeseries.data.points}
            majorEvents={timeseries.data.major_events}
          />
        )}
      </div>

      {/* Map + recent events side by side */}
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="glass-card overflow-hidden lg:col-span-2">
          <div className="border-b border-white/[0.06] px-6 py-4">
            <Tooltip
              content={
                <>
                  Each circle is a published fire event at an oil or fuel
                  facility. Circle size is proportional to the median (50th
                  percentile) emission estimate. Color indicates confidence
                  label. Click a circle for details. Basemap: CartoDB Dark Matter.
                </>
              }
            >
              <h2 className="text-sm font-medium text-slate-300">
                Fire events
              </h2>
            </Tooltip>
          </div>
          {events.isLoading ? (
            <div
              className="flex items-center justify-center bg-slate-900"
              style={{ minHeight: "460px" }}
            >
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
            </div>
          ) : (
            <EventMap events={mapEvents} />
          )}
        </div>

        <div className="glass-card flex flex-col">
          <div className="border-b border-white/[0.06] px-6 py-4">
            <Tooltip
              content={
                <>
                  The 10 most recently published fire events, sorted by date
                  descending. Each row links to the event detail page showing
                  full provenance chain, uncertainty distribution, and
                  methodology version.
                </>
              }
            >
              <h2 className="text-sm font-medium text-slate-300">
                Recent events
              </h2>
            </Tooltip>
          </div>
          <div className="flex-1 overflow-y-auto px-2">
            {events.isLoading ? (
              <div className="flex items-center justify-center py-8">
                <div className="h-4 w-4 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
              </div>
            ) : (
              <RecentEventsList events={recentEvents} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
