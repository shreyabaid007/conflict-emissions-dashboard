const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";
const USE_SNAPSHOT =
  process.env.NEXT_PUBLIC_USE_SNAPSHOT === "true" || API_BASE === "";

function snapshotUrl(path: string): string {
  // Map "/api/v1/events/abc?status=PUBLISHED" -> "/api-snapshot/events/abc.json"
  const [rawPath] = path.split("?");
  const stripped = rawPath.replace(/^\/api\/v1\//, "").replace(/\/$/, "");
  return `/api-snapshot/${stripped}.json`;
}

async function get<T>(path: string): Promise<T> {
  if (USE_SNAPSHOT) {
    const res = await fetch(snapshotUrl(path));
    if (!res.ok) {
      throw new Error(
        `Snapshot missing for ${path} (${res.status}). Re-run scripts/export_snapshot.py.`,
      );
    }
    return res.json() as Promise<T>;
  }
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    throw new Error(`API error: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export interface MetaResponse {
  methodology_version: string;
  last_data_update: string | null;
  event_count: number;
  facility_count: number;
}

export interface HeadlineResponse {
  total_p5: number;
  total_p50: number;
  total_p95: number;
  confirmed_event_count: number;
  facility_count: number;
  methodology_version: string;
}

export interface TimeseriesPoint {
  date: string;
  cumulative_p5: number;
  cumulative_p50: number;
  cumulative_p95: number;
}

export interface MajorEvent {
  date: string;
  label: string;
  event_id: string;
}

export interface TimeseriesResponse {
  points: TimeseriesPoint[];
  major_events: MajorEvent[];
}

export interface EventSummary {
  id: string;
  facility_name: string;
  facility_type: string;
  date: string;
  latitude: number;
  longitude: number;
  confidence: "CONFIRMED" | "VERIFIED" | "REPORTED";
  status: "published" | "pending_review" | "retracted";
  emission_p50: number | null;
}

export interface EmissionEstimate {
  id: string;
  event_id: string;
  methodology_version: string;
  method: string;
  p5: number;
  p50: number;
  p95: number;
  units: string;
  created_at: string;
}

export interface BackendEventSummary {
  id: string;
  facility_id: string;
  detected_at: string;
  last_seen_at: string;
  peak_frp_mw: number;
  total_frp_integral_mj: number | null;
  detection_source: string;
  confidence_label: string;
  status: string;
  notes: string | null;
  estimate: EmissionEstimate | null;
}

export interface EventDetailResponse {
  methodology_version: string;
  generated_at: string;
  data_license: string;
  attribution: string;
  data: BackendEventSummary;
  estimates: EmissionEstimate[];
}

export interface ProvenanceNode {
  node_type: "source" | "computation";
  id: string;
  detail: Record<string, unknown>;
}

export interface ProvenanceResponse {
  methodology_version: string;
  generated_at: string;
  event_id: string;
  chain: ProvenanceNode[];
  rendered: string;
}

export interface DamageAssessmentData {
  id: string;
  event_id: string;
  facility_id: string;
  fraction_destroyed_low: number;
  fraction_destroyed_mode: number;
  fraction_destroyed_high: number;
  assessed_by: string;
  assessment_method: string;
  notes: string | null;
  assessed_at: string;
  provenance_id: string;
}

export interface DamageAssessmentResponse {
  methodology_version: string;
  generated_at: string;
  event_id: string;
  data: DamageAssessmentData | null;
}

export interface FacilityDetail {
  data: {
    id: string;
    name: string;
    facility_type: string;
    country: string;
    capacity_barrels: number | null;
    operator: string | null;
  };
  geometry_wkt: string;
  event_count: number;
  total_p50_tco2e: number;
}

export interface AcledEvent {
  id: string;
  acled_id: number;
  event_date: string;
  event_type: string;
  sub_event_type: string | null;
  country: string;
  admin1: string | null;
  location: string | null;
  latitude: number;
  longitude: number;
  source: string | null;
  notes: string | null;
}

export interface EventDetail extends EventSummary {
  emission_p5: number | null;
  emission_p95: number | null;
  provenance: {
    source: string;
    description: string;
    timestamp?: string;
  }[];
  methodology_version: string;
}

export function fetchMeta(): Promise<MetaResponse> {
  return get<MetaResponse>("/api/v1/meta");
}

export function fetchHeadline(): Promise<HeadlineResponse> {
  return get<HeadlineResponse>("/api/v1/aggregates/headline");
}

export async function fetchTimeseries(): Promise<TimeseriesResponse> {
  const res = await get<{ data: { date: string; p5: number; p50: number; p95: number }[] }>("/api/v1/timeseries/cumulative");
  return {
    points: res.data.map((d) => ({
      date: d.date,
      cumulative_p5: d.p5,
      cumulative_p50: d.p50,
      cumulative_p95: d.p95,
    })),
    major_events: [],
  };
}

export async function fetchEvents(): Promise<EventSummary[]> {
  const [eventsRes, facilitiesRes] = await Promise.all([
    get<{ data: BackendEventSummary[] }>("/api/v1/events?status=PUBLISHED&per_page=200"),
    get<{ data: { id: string; name: string; facility_type: string; latitude: number | null; longitude: number | null }[] }>("/api/v1/facilities?per_page=200"),
  ]);
  const facilityMap = new Map(
    facilitiesRes.data.map((f) => [f.id, f]),
  );
  return eventsRes.data.map((e) => {
    const facility = facilityMap.get(e.facility_id);
    return {
      id: e.id,
      facility_name: facility?.name ?? e.facility_id,
      facility_type: facility?.facility_type ?? "",
      date: e.detected_at.slice(0, 10),
      latitude: facility?.latitude ?? 0,
      longitude: facility?.longitude ?? 0,
      confidence: e.confidence_label as EventSummary["confidence"],
      status: e.status.toLowerCase() as EventSummary["status"],
      emission_p50: e.estimate?.p50 ?? null,
    };
  });
}

export function fetchEvent(id: string): Promise<EventDetail> {
  return get<EventDetail>(`/api/v1/events/${id}`);
}

export function fetchEventDetail(id: string): Promise<EventDetailResponse> {
  return get<EventDetailResponse>(`/api/v1/events/${id}`);
}

export function fetchEventProvenance(id: string): Promise<ProvenanceResponse> {
  return get<ProvenanceResponse>(`/api/v1/events/${id}/provenance`);
}

export function fetchEventAssessment(id: string): Promise<DamageAssessmentResponse> {
  return get<DamageAssessmentResponse>(`/api/v1/events/${id}/assessment`);
}

export function fetchFacility(id: string): Promise<FacilityDetail> {
  return get<FacilityDetail>(`/api/v1/facilities/${id}`);
}

export function fetchEventAcled(eventId: string): Promise<AcledEvent[]> {
  return get<AcledEvent[]>(`/api/v1/events/${eventId}/acled`);
}

export interface MethodologyVersionResponse {
  generated_at: string;
  version_id: string;
  released_at: string;
  pdf_url: string;
}

export interface ChangelogEntryResponse {
  version_id: string | null;
  event_id: string | null;
  change_type: string;
  detail: string;
  occurred_at: string;
}

export interface ChangelogResponse {
  generated_at: string;
  entries: ChangelogEntryResponse[];
}

export function fetchMethodology(): Promise<MethodologyVersionResponse> {
  return get<MethodologyVersionResponse>("/api/v1/methodology/current");
}

export function fetchChangelog(): Promise<ChangelogResponse> {
  return get<ChangelogResponse>("/api/v1/changelog");
}

// --- Standalone provenance (gap C.8) ---------------------------------------

export interface ProvenanceChainResponse {
  methodology_version: string;
  generated_at: string;
  data_license: string;
  attribution: string;
  provenance_id: string;
  chain: ProvenanceNode[];
  rendered: string;
}

export function fetchProvenance(id: string): Promise<ProvenanceChainResponse> {
  return get<ProvenanceChainResponse>(`/api/v1/provenance/${id}`);
}

// --- Public revision log (gap 1.5) -----------------------------------------

export interface RevisionEntry {
  id: string;
  target_type: string;
  target_id: string;
  from_state: string;
  to_state: string;
  action: string;
  actor: string;
  reason: string | null;
  public_note: string | null;
  methodology_version: string | null;
  created_at: string;
}

export interface RevisionLogResponse {
  methodology_version: string;
  generated_at: string;
  data_license: string;
  attribution: string;
  data: RevisionEntry[];
  pagination: { total: number; page: number; per_page: number; pages: number };
}

export function fetchRevisions(targetId?: string): Promise<RevisionLogResponse> {
  const query = targetId ? `?target_id=${targetId}` : "";
  return get<RevisionLogResponse>(`/api/v1/revisions${query}`);
}
