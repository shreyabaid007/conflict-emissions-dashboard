"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchEvents } from "@/lib/api";
import { EventMap } from "@/components/EventMap";

export default function MapPage() {
  const { data: events, isLoading } = useQuery({
    queryKey: ["events"],
    queryFn: fetchEvents,
  });

  const published =
    events?.filter(
      (e) => e.status === "published" && e.emission_p50 != null,
    ) ?? [];

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col">
      <div className="border-b border-white/[0.06] px-6 py-4">
        <h1 className="text-lg font-semibold text-white">Event Map</h1>
        <p className="mt-0.5 text-xs text-slate-500">
          All published fire events at oil and fuel infrastructure with emission
          estimates. Circle size proportional to median emission. Click an event
          for details.
        </p>
      </div>
      <div className="flex-1">
        {isLoading ? (
          <div className="flex h-full items-center justify-center bg-slate-950">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
          </div>
        ) : (
          <EventMap events={published} className="!min-h-full" />
        )}
      </div>
    </div>
  );
}
