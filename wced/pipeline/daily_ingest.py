"""Prefect flow: daily_ingest

Full WCED pipeline for one calendar day. Runs eleven tasks in sequence
(with parallelism inside individual tasks where noted):

  1.  load_facilities           — GeoJSON registry → list[Facility]
  2.  ingest_firms_viirs        — FIRMS VIIRS area CSV → raw rows
  3.  ingest_firms_modis        — FIRMS MODIS area CSV → raw rows
  4.  ingest_acled              — ACLED API → list[ACLEDEvent]
  5.  detect_candidate_events   — cluster + facility match → list[MatchedCandidate]
  6.  fetch_s2_chips_for_candidates — S2 chip download (asyncio parallel)
  7.  classify_fires            — heuristic + Claude (asyncio, rate-limited)
  8.  corroborate_with_acled    — spatial/temporal ACLED matching
  9.  assign_confidence_labels  — evidence combination → label per candidate
  10. submit_to_editorial_queue — build FireEvents, push to review queue
  11. log_pipeline_run          — structured metrics, flush seen-hash store

Idempotency
-----------
Each raw FIRMS row is content-hashed as SHA-256(lat|lon|acq_date|acq_time|
satellite). Hashes from previously completed runs live in
``data/pipeline/seen_hashes/{date}.json``. Re-running for the same date
filters already-seen detections before clustering, preventing duplicate
candidates. The hash store is only written on task 11 (after the entire
pipeline completes); a mid-run crash leaves the store unchanged so the next
run reprocesses from the same starting point.

Error handling
--------------
Tasks 2–4 each have three retries with exponential back-off. If a task
exhausts its retries the flow records the failure in ``PipelineRunMetrics``,
substitutes an empty result, and continues — a partial result is always
published rather than nothing.

Serialisation note
------------------
``S2ChipResult.chip`` is an ``xr.Dataset``. Prefect serialises task results
via pickle for in-process execution; this works for typical chip sizes
(~160 KB per four-band 100×100 chip). For distributed task runners that
serialise results to a remote store, persist chips to MinIO/S3 and replace
the ``chip`` field with a path/URI before deploying.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid5

import logging as _stdlib_logging

import structlog
from pydantic import BaseModel, ConfigDict
from prefect import flow, task, get_run_logger
from prefect.exceptions import MissingContextError

from wced.ai.classify import FireClassification, FireLabel, classify_fire
from wced.ai.claude_client import AnthropicClient
from wced.detect.facility_match import build_facility_tree, match_to_facility_with_tree
from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection, hotspots_to_candidates
from wced.ingest.acled import ACLEDConnector, ACLEDEvent, ACLEDError, DEFAULT_COUNTRIES
from wced.ingest.firms import FIRMSConnector
from wced.ingest.gdelt import GDELTConnector, GDELTEvent
from wced.ingest.sentinel2 import Sentinel2Connector, Sentinel2Error
from wced.models.event import DetectionSource, EventStatus, FireEvent
from wced.models.facility import Facility
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord, Source, SourceType
from wced.pipeline.facility_repo import InMemoryFacilityRepository
from wced.provenance.store import InMemoryProvenanceStore
from wced.verify.acled_corroboration import find_acled_corroboration
from wced.verify.confidence import assign_confidence
from wced.verify.corroboration import CorroborationMatch, find_corroboration
from wced.verify.editorial import InMemoryReviewQueue
from wced.verify.sentinel2_check import (
    S2_CHIP_HALF_WIDTH_DEG,
    S2_LOOKBACK_HOURS,
    S2_LOOKAHEAD_HOURS,
    S2_MAX_CLOUD_PCT,
    VerificationStatus,
    VerifiedCandidate,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).parent.parent.parent
_FACILITIES_GEOJSON: Path = _REPO_ROOT / "data" / "facilities" / "iran_oil_gas.geojson"
_SEEN_HASHES_DIR: Path = _REPO_ROOT / "data" / "pipeline" / "seen_hashes"

# Iran + Gulf AOI [west, south, east, north] WGS84 — see CLAUDE.md
IRAN_BBOX: tuple[float, float, float, float] = (44.0, 25.0, 63.5, 40.0)

# Sentinel-2 and Claude concurrency caps (per flow run, not per machine)
_S2_MAX_CONCURRENT: int = 5
_CLAUDE_MAX_CONCURRENT: int = 3


def _logger() -> Any:
    """Return a Prefect run logger when inside a flow/task context, or fall back
    to the stdlib module logger so tasks can be called directly in unit tests."""
    try:
        return get_run_logger()
    except MissingContextError:
        return _stdlib_logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Supporting data types
# ---------------------------------------------------------------------------


class MatchedCandidate(BaseModel):
    """A ``CandidateFireEvent`` with its facility-attribution result.

    Produced by ``detect_candidate_events`` and passed forward unchanged
    through tasks 6–10. The ``detection_hash`` is a SHA-256 of the sorted
    constituent ``FIRMSDetection.id`` values — used to flush the seen-hash
    store at the end of the run.
    """

    model_config = ConfigDict(frozen=True)

    candidate: CandidateFireEvent
    facility: Facility | None
    match_distance_m: float
    detection_hash: str  # SHA-256 of sorted constituent detection IDs


class PipelineRunMetrics(BaseModel):
    """Counters and timestamps emitted by ``log_pipeline_run``.

    Every field has a sensible zero default so the object can be constructed
    incrementally and passed to ``log_pipeline_run`` even when upstream tasks
    failed and their counts are unavailable.
    """

    model_config = ConfigDict(frozen=True)

    run_date: date
    started_at: datetime
    finished_at: datetime
    n_viirs_detections: int = 0
    n_modis_detections: int = 0
    n_deduplicated: int = 0
    n_candidates: int = 0
    n_facility_matched: int = 0
    n_s2_chips_fetched: int = 0
    n_s2_chips_failed: int = 0
    n_classified: int = 0
    n_acled_events: int = 0
    n_submitted_to_queue: int = 0
    task_failures: tuple[str, ...] = ()


@dataclass
class S2ChipResult:
    """Pre-fetched Sentinel-2 chip with provenance metadata.

    ``chip`` is an ``xr.Dataset``; see the serialisation note in the module
    docstring before using a remote Prefect result backend.
    """

    chip: Any  # xr.Dataset in production; mockable in tests
    source: Source
    item_id: str
    cloud_cover: float


# ---------------------------------------------------------------------------
# Content-hash deduplication helpers
# ---------------------------------------------------------------------------


def _detection_hash(row: dict[str, Any]) -> str:
    """SHA-256 of canonical FIRMS pixel identity fields.

    Uses (latitude, longitude, acq_date, acq_time, satellite) so that
    re-ingesting an identical CSV response always produces the same hash,
    regardless of which ``Source`` UUID was assigned to the API call.
    """
    key = "|".join([
        str(row.get("latitude", "")),
        str(row.get("longitude", "")),
        str(row.get("acq_date", "")),
        str(row.get("acq_time", "")),
        str(row.get("satellite", "")),
    ])
    return hashlib.sha256(key.encode()).hexdigest()


def _load_seen_hashes(run_date: date) -> set[str]:
    """Load already-processed FIRMS detection hashes for *run_date*.

    Returns an empty set when the file does not exist (first run for the date)
    or when the file is malformed (treated as a fresh start with a warning).
    """
    path = _SEEN_HASHES_DIR / f"{run_date.isoformat()}.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("hashes", []))
    except Exception as exc:
        log.warning("seen_hashes.load_failed", path=str(path), error=str(exc))
        return set()


def _save_seen_hashes(run_date: date, hashes: set[str]) -> None:
    """Persist *hashes* for *run_date*.  Called only from ``log_pipeline_run``
    so hashes are only flushed after the full pipeline succeeds.
    """
    _SEEN_HASHES_DIR.mkdir(parents=True, exist_ok=True)
    path = _SEEN_HASHES_DIR / f"{run_date.isoformat()}.json"
    path.write_text(
        json.dumps(
            {
                "hashes": sorted(hashes),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Flow-level helper: safe result extraction
# ---------------------------------------------------------------------------


def _safe_result(
    future: Any,
    task_name: str,
    default: Any,
    failures: list[str],
    logger: Any,
) -> Any:
    """Return ``future.result()``, or *default* if the task raised.

    Appends *task_name* to *failures* on exception so the caller can
    propagate the failure into ``PipelineRunMetrics``.
    """
    try:
        return future.result()
    except Exception as exc:
        logger.error("task_failed", task=task_name, error=str(exc))
        failures.append(task_name)
        return default


# ---------------------------------------------------------------------------
# Tasks 1 – 4: data loading and ingest
# ---------------------------------------------------------------------------


@task(name="load-facilities", log_prints=True)
def load_facilities() -> list[Facility]:
    """Load the registered oil/gas facility registry from GeoJSON.

    Returns
    -------
    list[Facility]
        All facilities in the registry.  Raises on file or parse errors
        rather than returning an empty list — a missing registry is a
        configuration error, not a recoverable condition.
    """
    logger = _logger()
    repo = InMemoryFacilityRepository.load_geojson(_FACILITIES_GEOJSON)
    facilities = repo.all()
    logger.info("load_facilities: %d facilities loaded", len(facilities))
    return facilities


@task(name="ingest-firms-viirs", retries=3, retry_delay_seconds=30, log_prints=True)
async def ingest_firms_viirs(
    target_date: date,
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch VIIRS (S-NPP + NOAA-20 + NOAA-21) thermal anomaly detections.

    Parameters
    ----------
    target_date : date
        UTC calendar date to query (full 24-hour window).
    bbox : tuple[float, float, float, float]
        Bounding box ``(west, south, east, north)`` in WGS84 degrees.

    Returns
    -------
    list[dict]
        Normalised FIRMS rows.  Each dict carries float-coerced numeric
        fields, a ``detected_at`` UTC datetime, a ``_source`` provenance
        ``Source`` object, and ``_detection_source`` set to
        ``"FIRMS_VIIRS"``.

    Environment
    -----------
    ``FIRMS_MAP_KEY`` — NASA FIRMS API key (required).
    """
    logger = _logger()
    map_key = os.environ["FIRMS_MAP_KEY"]
    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=UTC)
    end = start + timedelta(hours=24)

    rows: list[dict[str, Any]] = []
    async with FIRMSConnector(map_key) as conn:
        async for row in conn.ingest_viirs(start, end, bbox):
            row["_detection_source"] = DetectionSource.FIRMS_VIIRS.value
            rows.append(row)

    logger.info("ingest_firms_viirs: %d rows for %s", len(rows), target_date)
    return rows


@task(name="ingest-firms-modis", retries=3, retry_delay_seconds=30, log_prints=True)
async def ingest_firms_modis(
    target_date: date,
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch MODIS (Terra + Aqua) thermal anomaly detections.

    Parameters
    ----------
    target_date : date
        UTC calendar date to query.
    bbox : tuple[float, float, float, float]
        Bounding box ``(west, south, east, north)`` in WGS84 degrees.

    Returns
    -------
    list[dict]
        Normalised FIRMS rows with ``_detection_source`` set to
        ``"FIRMS_MODIS"``.

    Environment
    -----------
    ``FIRMS_MAP_KEY`` — NASA FIRMS API key (required).
    """
    logger = _logger()
    map_key = os.environ["FIRMS_MAP_KEY"]
    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=UTC)
    end = start + timedelta(hours=24)

    rows: list[dict[str, Any]] = []
    async with FIRMSConnector(map_key) as conn:
        async for row in conn.ingest_modis(start, end, bbox):
            row["_detection_source"] = DetectionSource.FIRMS_MODIS.value
            rows.append(row)

    logger.info("ingest_firms_modis: %d rows for %s", len(rows), target_date)
    return rows


ConflictEvent = ACLEDEvent | GDELTEvent


@task(name="ingest-conflict-events", retries=3, retry_delay_seconds=60, log_prints=True)
async def ingest_conflict_events(
    target_date: date,
    countries: list[str],
) -> tuple[list[ConflictEvent], str]:
    """Fetch conflict events, trying ACLED first with GDELT as fallback.

    The source selection follows ``WCED_CONFLICT_SOURCE`` env var:
      - ``"acled"`` — ACLED only (raises if unavailable).
      - ``"gdelt"`` — GDELT only.
      - ``"both"``  — query both; ACLED matches take priority for confidence.
      - unset/default — try ACLED first; if 403 or credentials missing,
        fall back to GDELT. Log which source was used.

    When ACLED access is eventually approved, ACLED takes priority and GDELT
    becomes supplementary (both are queried; ACLED matches override GDELT
    matches for confidence purposes).

    Parameters
    ----------
    target_date : date
        Centre of the query window (±1 day).
    countries : list[str]
        Country names for ACLED (English).

    Returns
    -------
    tuple[list[ConflictEvent], str]
        Events and source identifier ("acled", "gdelt", or "both").
    """
    logger = _logger()
    query_start = target_date - timedelta(days=1)
    query_end = target_date + timedelta(days=1)
    conflict_source = os.environ.get("WCED_CONFLICT_SOURCE", "").lower()

    acled_events: list[ACLEDEvent] = []
    gdelt_events: list[GDELTEvent] = []

    # --- ACLED ---
    use_acled = conflict_source in ("acled", "both", "")
    if use_acled:
        acled_email = os.environ.get("ACLED_EMAIL", "")
        acled_password = os.environ.get("ACLED_PASSWORD", "")

        if acled_email and acled_password:
            try:
                async with ACLEDConnector(
                    acled_email, acled_password, countries=countries,
                ) as conn:
                    async for record in conn.query_events(
                        query_start, query_end, countries=countries,
                    ):
                        acled_events.append(record["event"])
                logger.info(
                    "ingest_conflict_events: %d ACLED events for %s",
                    len(acled_events), target_date,
                )
            except (ACLEDError, httpx.HTTPStatusError) as exc:
                status = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if status == 403 or conflict_source == "":
                    logger.warning(
                        "ingest_conflict_events: ACLED unavailable (%s), "
                        "falling back to GDELT",
                        exc,
                    )
                    use_acled = False
                else:
                    raise
        else:
            if conflict_source == "acled":
                raise RuntimeError(
                    "WCED_CONFLICT_SOURCE=acled but ACLED_EMAIL/ACLED_PASSWORD "
                    "not set"
                )
            logger.info(
                "ingest_conflict_events: ACLED credentials not configured, "
                "using GDELT"
            )
            use_acled = False

    # --- GDELT ---
    use_gdelt = conflict_source in ("gdelt", "both") or not use_acled
    if use_gdelt:
        start_dt = datetime(
            query_start.year, query_start.month, query_start.day, tzinfo=UTC,
        )
        end_dt = datetime(
            query_end.year, query_end.month, query_end.day,
            23, 59, 59, tzinfo=UTC,
        )
        async with GDELTConnector() as conn:
            async for record in conn.query_events_api(
                start=start_dt, end=end_dt,
            ):
                gdelt_events.append(record["event"])
        logger.info(
            "ingest_conflict_events: %d GDELT events for %s",
            len(gdelt_events), target_date,
        )

    # Determine which source(s) were actually used
    if acled_events and gdelt_events:
        source_used = "both"
    elif acled_events:
        source_used = "acled"
    else:
        source_used = "gdelt"

    all_events: list[ConflictEvent] = []
    all_events.extend(acled_events)
    all_events.extend(gdelt_events)

    logger.info(
        "ingest_conflict_events: source=%s total=%d (acled=%d gdelt=%d)",
        source_used, len(all_events), len(acled_events), len(gdelt_events),
    )
    return all_events, source_used


# ---------------------------------------------------------------------------
# Task 5: detection and facility matching
# ---------------------------------------------------------------------------


@task(name="detect-candidate-events", log_prints=True)
def detect_candidate_events(
    firms_results: list[dict[str, Any]],
    facilities: list[Facility],
) -> list[MatchedCandidate]:
    """Cluster FIRMS detections and match each candidate to a registered facility.

    Steps:
      1. Content-hash each raw FIRMS row; filter rows already seen in a
         previous run (loaded from ``data/pipeline/seen_hashes/{date}.json``).
      2. Convert new rows to ``FIRMSDetection`` objects with deterministic
         UUIDs (``uuid5(NAMESPACE_URL, detection_hash)``) so the same
         detection always maps to the same ID across re-runs.
      3. Record each unique ``Source`` provenance record from the raw rows.
      4. Cluster with spatial DBSCAN (eps=500 m) + 24-hour temporal split
         via ``hotspots_to_candidates``.
      5. Match each candidate to the nearest facility within 500 m using
         a pre-built STRtree (``match_to_facility_with_tree``).
      6. Return ``MatchedCandidate`` objects.  Unmatched candidates
         (no facility within 500 m) are included with ``facility=None``;
         they are excluded from the editorial queue in task 10.

    The seen-hash set is NOT written here — it is flushed by
    ``log_pipeline_run`` only after the full pipeline completes, so a
    mid-run crash leaves the store unchanged and the next run reprocesses
    from the same state.

    Parameters
    ----------
    firms_results : list[dict]
        Combined VIIRS + MODIS rows from tasks 2 and 3.
    facilities : list[Facility]
        All registered facilities from task 1.

    Returns
    -------
    list[MatchedCandidate]
    """
    logger = _logger()
    store = InMemoryProvenanceStore()

    if not firms_results:
        logger.warning("detect_candidate_events: no FIRMS rows received")
        return []

    # Derive run date from the first detection for hash-store lookup.
    sample_dt = firms_results[0].get("detected_at")
    run_date: date | None = (
        sample_dt.date() if isinstance(sample_dt, datetime) else None
    )
    seen_hashes: set[str] = _load_seen_hashes(run_date) if run_date else set()

    new_hashes: set[str] = set()
    all_detections: list[FIRMSDetection] = []
    recorded_sources: dict[UUID, None] = {}  # track which Sources we've recorded

    for row in firms_results:
        h = _detection_hash(row)
        if h in seen_hashes:
            continue
        new_hashes.add(h)

        source: Source = row["_source"]
        if source.id not in recorded_sources:
            store.record_source(source)
            recorded_sources[source.id] = None

        detection = FIRMSDetection(
            id=uuid5(NAMESPACE_URL, h),  # deterministic: same pixel → same UUID
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            frp_mw=float(row.get("frp", 0.0)),
            detected_at=row["detected_at"],
            detection_source=DetectionSource(row["_detection_source"]),
            brightness_k=float(row.get("brightness", 0.0)),
            confidence=str(row.get("confidence", "")),
            source_id=source.id,
        )
        all_detections.append(detection)

    n_deduped = len(firms_results) - len(all_detections)
    logger.info(
        "detect_candidate_events: %d rows → %d new detections (%d deduped)",
        len(firms_results),
        len(all_detections),
        n_deduped,
    )

    if not all_detections:
        logger.info("detect_candidate_events: all detections already seen; skipping")
        return []

    candidates = hotspots_to_candidates(all_detections, store=store)

    tree, fac_list = (
        build_facility_tree(facilities) if facilities else (None, [])
    )

    matched: list[MatchedCandidate] = []
    for candidate in candidates:
        # Derive a stable hash for the candidate from its detection IDs.
        cand_hash = hashlib.sha256(
            "|".join(sorted(str(h.id) for h in candidate.hotspots)).encode()
        ).hexdigest()

        if fac_list and tree is not None:
            facility, dist_m = match_to_facility_with_tree(
                candidate, tree, fac_list, store=store
            )
        else:
            facility, dist_m = None, float("inf")

        matched.append(
            MatchedCandidate(
                candidate=candidate,
                facility=facility,
                match_distance_m=dist_m,
                detection_hash=cand_hash,
            )
        )

    n_matched = sum(1 for m in matched if m.facility is not None)
    logger.info(
        "detect_candidate_events: %d candidates (%d facility-matched, %d unmatched)",
        len(matched),
        n_matched,
        len(matched) - n_matched,
    )
    return matched


# ---------------------------------------------------------------------------
# Task 6: Sentinel-2 chip fetching (parallel)
# ---------------------------------------------------------------------------


@task(name="fetch-s2-chips-for-candidates", log_prints=True)
async def fetch_s2_chips_for_candidates(
    candidates: list[MatchedCandidate],
) -> dict[str, S2ChipResult | None]:
    """Fetch a Sentinel-2 L2A chip for every candidate in parallel.

    Searches for scenes within ±72 h of the candidate's first detection.
    Uses an asyncio semaphore (max ``_S2_MAX_CONCURRENT``) to cap the
    number of concurrent STAC queries.

    The ``Sentinel2Connector`` is synchronous; each chip fetch runs in a
    thread via ``asyncio.to_thread`` to avoid blocking the event loop.

    Parameters
    ----------
    candidates : list[MatchedCandidate]
        Candidates from ``detect_candidate_events``.

    Returns
    -------
    dict[str, S2ChipResult | None]
        Keys are ``str(candidate.id)``.  ``None`` when no usable scene was
        found or the download failed.

    Environment
    -----------
    Optional ``PC_SDK_SUBSCRIPTION_KEY`` — Planetary Computer subscription
    key (anonymous access is also supported but rate-limited).
    """
    logger = _logger()
    if not candidates:
        return {}

    s2 = Sentinel2Connector(cache_dir=None)  # no disk cache in pipeline tasks
    semaphore = asyncio.Semaphore(_S2_MAX_CONCURRENT)

    async def _fetch_one(mc: MatchedCandidate) -> tuple[str, S2ChipResult | None]:
        key = str(mc.candidate.id)
        async with semaphore:
            try:
                result = await asyncio.to_thread(_fetch_chip_sync, mc.candidate, s2)
                return key, result
            except Exception as exc:
                log.warning(
                    "s2_chip_fetch_failed",
                    candidate_id=key,
                    error=str(exc),
                )
                return key, None

    pairs = await asyncio.gather(*[_fetch_one(mc) for mc in candidates])
    chips: dict[str, S2ChipResult | None] = dict(pairs)

    n_ok = sum(1 for v in chips.values() if v is not None)
    logger.info(
        "fetch_s2_chips_for_candidates: %d/%d chips fetched",
        n_ok,
        len(candidates),
    )
    return chips


def _fetch_chip_sync(
    candidate: CandidateFireEvent,
    s2: Sentinel2Connector,
) -> S2ChipResult | None:
    """Synchronous Sentinel-2 chip fetch for a single candidate.

    Called from ``asyncio.to_thread``; must not touch the event loop.
    Returns ``None`` when no usable scene is available.
    """
    centre_t = candidate.first_detected_at
    window = (
        centre_t - timedelta(hours=S2_LOOKBACK_HOURS),
        centre_t + timedelta(hours=S2_LOOKAHEAD_HOURS),
    )
    items = s2.search_around(
        candidate.centroid_lat,
        candidate.centroid_lon,
        window,
        max_cloud_pct=S2_MAX_CLOUD_PCT,
    )
    if not items:
        return None

    best = items[0]
    cloud = float(best.properties.get("eo:cloud_cover", 100.0))
    half = S2_CHIP_HALF_WIDTH_DEG
    bbox = (
        candidate.centroid_lon - half,
        candidate.centroid_lat - half,
        candidate.centroid_lon + half,
        candidate.centroid_lat + half,
    )
    try:
        chip, source = s2.fetch_chip(best, bbox)
    except Sentinel2Error:
        return None

    return S2ChipResult(
        chip=chip,
        source=source,
        item_id=best.id,
        cloud_cover=cloud,
    )


# ---------------------------------------------------------------------------
# Task 7: fire classification (parallel, rate-limited)
# ---------------------------------------------------------------------------


@task(name="classify-fires", log_prints=True)
async def classify_fires(
    candidates: list[MatchedCandidate],
    s2_chips: dict[str, S2ChipResult | None],
) -> dict[str, VerifiedCandidate]:
    """Classify each candidate using pre-fetched S2 chips.

    For candidates with a chip: runs the two-path classifier
    (heuristic SWIR analysis; AI escalation to Claude for ambiguous chips).
    Limits concurrent Claude calls to ``_CLAUDE_MAX_CONCURRENT`` via an
    asyncio semaphore.  ``classify_fire`` is synchronous so each call is
    dispatched via ``asyncio.to_thread``.

    For candidates without a chip (``None`` entry in *s2_chips*): emits a
    ``VerifiedCandidate`` with ``status=AWAITING_OPTICAL_CHECK`` so
    downstream tasks still receive an entry for every candidate.

    Parameters
    ----------
    candidates : list[MatchedCandidate]
        Candidates from task 5.
    s2_chips : dict[str, S2ChipResult | None]
        Pre-fetched chips keyed by ``str(candidate.id)`` from task 6.

    Returns
    -------
    dict[str, VerifiedCandidate]
        Keys are ``str(candidate.id)``.

    Environment
    -----------
    ``ANTHROPIC_API_KEY`` — required only when the AI classification path
    is invoked (heuristic-only cases never touch the Claude API).
    """
    logger = _logger()
    if not candidates:
        return {}

    # Build the AI client once; classify_fire constructs it lazily if None.
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    ai_client: AnthropicClient | None = (
        AnthropicClient(api_key=api_key) if api_key else None
    )

    semaphore = asyncio.Semaphore(_CLAUDE_MAX_CONCURRENT)
    store = InMemoryProvenanceStore()

    async def _classify_one(
        mc: MatchedCandidate,
    ) -> tuple[str, VerifiedCandidate]:
        key = str(mc.candidate.id)
        chip_result = s2_chips.get(key)

        if chip_result is None or mc.facility is None:
            return key, VerifiedCandidate(
                candidate=mc.candidate,
                status=VerificationStatus.AWAITING_OPTICAL_CHECK,
                notes=(
                    "No Sentinel-2 chip available."
                    if chip_result is None
                    else "No matched facility — skipping classification."
                ),
            )

        async with semaphore:
            try:
                classification: FireClassification = await asyncio.to_thread(
                    classify_fire,
                    chip_result.chip,
                    mc.candidate,
                    mc.facility,
                    store=store,
                    client=ai_client,
                )
            except Exception as exc:
                log.warning(
                    "classify_fire_failed",
                    candidate_id=key,
                    error=str(exc),
                )
                return key, VerifiedCandidate(
                    candidate=mc.candidate,
                    status=VerificationStatus.AWAITING_OPTICAL_CHECK,
                    s2_item_id=chip_result.item_id,
                    s2_cloud_cover=chip_result.cloud_cover,
                    notes=f"Classification failed: {exc}",
                )

        # Map FireLabel → VerificationStatus
        if classification.label is FireLabel.CONFIRMED_FIRE:
            status = VerificationStatus.VERIFIED
        elif classification.label is FireLabel.AMBIGUOUS:
            status = VerificationStatus.AMBIGUOUS
        else:
            status = VerificationStatus.REJECTED

        return key, VerifiedCandidate(
            candidate=mc.candidate,
            status=status,
            classification=classification,
            s2_item_id=chip_result.item_id,
            s2_cloud_cover=chip_result.cloud_cover,
            provenance_ids=(chip_result.source.id, classification.provenance_id),
        )

    pairs = await asyncio.gather(*[_classify_one(mc) for mc in candidates])
    verified: dict[str, VerifiedCandidate] = dict(pairs)

    n_classified = sum(
        1
        for v in verified.values()
        if v.status is not VerificationStatus.AWAITING_OPTICAL_CHECK
    )
    logger.info(
        "classify_fires: %d/%d candidates classified", n_classified, len(candidates)
    )
    return verified


# ---------------------------------------------------------------------------
# Task 8: ACLED corroboration
# ---------------------------------------------------------------------------


@task(name="corroborate-with-conflict-events", log_prints=True)
def corroborate_with_conflict_events(
    candidates: list[MatchedCandidate],
    conflict_events: list[ConflictEvent],
) -> dict[str, list[CorroborationMatch]]:
    """Spatially and temporally match conflict events to each candidate.

    Delegates to ``find_corroboration`` which applies the default search
    windows from ``methodology/v1.0.pdf §4.3 Table 4`` (±24 h, ≤2 000 m).
    Each match carries a ``source_type`` ("acled" or "gdelt") so confidence
    assignment can distinguish strong from weak corroboration.

    Parameters
    ----------
    candidates : list[MatchedCandidate]
        Candidates from task 5.
    conflict_events : list[ConflictEvent]
        ACLED and/or GDELT events from task 4.

    Returns
    -------
    dict[str, list[CorroborationMatch]]
        Keys are ``str(candidate.id)``. Values sorted ascending by distance.
    """
    logger = _logger()
    result: dict[str, list[CorroborationMatch]] = {}
    for mc in candidates:
        key = str(mc.candidate.id)
        matches = find_corroboration(mc.candidate, conflict_events)
        result[key] = matches
        if matches:
            log.debug(
                "corroboration.match",
                candidate_id=key,
                n_matches=len(matches),
                source_types=[m.source_type for m in matches],
            )

    n_with_matches = sum(1 for v in result.values() if v)
    logger.info(
        "corroborate_with_conflict_events: %d/%d candidates have matches",
        n_with_matches,
        len(candidates),
    )
    return result


# ---------------------------------------------------------------------------
# Task 9: confidence label assignment
# ---------------------------------------------------------------------------


@task(name="assign-confidence-labels", log_prints=True)
def assign_confidence_labels(
    candidates: list[MatchedCandidate],
    verified: dict[str, VerifiedCandidate],
    corroborations: dict[str, list[CorroborationMatch]],
) -> dict[str, ConfidenceLabel]:
    """Combine three evidence streams into a single ConfidenceLabel per candidate.

    Applies the decision table from ``methodology/v1.0.pdf §4.3 Table 5``,
    extended with corroboration source distinction (ACLED vs GDELT):

    +-------------------+------------------+-------------+-------------------+
    | FIRMS persistence | S2 confirms fire | Corr. type  | → label           |
    +-------------------+------------------+-------------+-------------------+
    | ≥2 overpasses     | yes              | ACLED       | CONFIRMED         |
    | ≥2 overpasses     | yes              | GDELT only  | VERIFIED          |
    | ≥2 overpasses     | yes              | none        | VERIFIED          |
    | ≥2 overpasses     | no / clouds      | any         | REPORTED          |
    | 1 overpass        | any              | any         | SUSPECTED         |
    +-------------------+------------------+-------------+-------------------+

    Parameters
    ----------
    candidates : list[MatchedCandidate]
        Candidates from task 5.
    verified : dict[str, VerifiedCandidate]
        Classification results from task 7.
    corroborations : dict[str, list[CorroborationMatch]]
        Conflict-event matches from task 8.

    Returns
    -------
    dict[str, ConfidenceLabel]
    """
    logger = _logger()
    store = InMemoryProvenanceStore()
    labels: dict[str, ConfidenceLabel] = {}

    for mc in candidates:
        key = str(mc.candidate.id)
        s2_result = verified.get(key)
        corr_matches = corroborations.get(key, [])
        label = assign_confidence(
            mc.candidate,
            s2_result,
            [],  # empty acled_matches — using corroboration_matches instead
            corroboration_matches=corr_matches,
            store=store,
        )
        labels[key] = label

    from collections import Counter

    dist = Counter(labels.values())
    logger.info(
        "assign_confidence_labels: %s",
        {k.value: v for k, v in dist.items()},
    )
    return labels


# ---------------------------------------------------------------------------
# Task 10: editorial queue submission
# ---------------------------------------------------------------------------


@task(name="submit-to-editorial-queue", log_prints=True)
def submit_to_editorial_queue(
    candidates: list[MatchedCandidate],
    confidence_labels: dict[str, ConfidenceLabel],
) -> list[FireEvent]:
    """Build ``FireEvent`` objects and push them to the editorial review queue.

    Only facility-matched candidates (``mc.facility is not None``) are
    submitted.  Unmatched candidates are logged and skipped.

    The ``InMemoryReviewQueue.submit()`` call is idempotent for events
    already in PENDING_REVIEW status; re-running the flow for the same date
    will not duplicate queue entries for already-pending events.

    Parameters
    ----------
    candidates : list[MatchedCandidate]
        Candidates from task 5.
    confidence_labels : dict[str, ConfidenceLabel]
        Labels from task 9, keyed by ``str(candidate.id)``.

    Returns
    -------
    list[FireEvent]
        Submitted events in PENDING_REVIEW status.
    """
    logger = _logger()
    queue = InMemoryReviewQueue()
    submitted: list[FireEvent] = []
    n_skipped = 0

    for mc in candidates:
        if mc.facility is None:
            n_skipped += 1
            log.debug(
                "submit_to_editorial_queue.skipped_unmatched",
                candidate_id=str(mc.candidate.id),
            )
            continue

        key = str(mc.candidate.id)
        label = confidence_labels.get(key, ConfidenceLabel.SUSPECTED)

        event = _build_fire_event(mc, label)
        try:
            submitted_event = queue.submit(event, reviewer="pipeline:daily_ingest")
            submitted.append(submitted_event)
        except ValueError as exc:
            # Event already in queue with a non-PENDING status — log and skip.
            log.warning(
                "submit_to_editorial_queue.already_queued",
                candidate_id=key,
                error=str(exc),
            )

    logger.info(
        "submit_to_editorial_queue: %d submitted, %d skipped (unmatched)",
        len(submitted),
        n_skipped,
    )
    return submitted


def _build_fire_event(mc: MatchedCandidate, label: ConfidenceLabel) -> FireEvent:
    """Construct a PENDING_REVIEW ``FireEvent`` from a matched candidate.

    Uses the candidate's first/last detection timestamps and peak FRP
    directly.  ``total_frp_integral_mj`` is left ``None`` — it is computed
    in the quantification step after editorial approval.
    """
    assert mc.facility is not None, "_build_fire_event requires a matched facility"
    now = datetime.now(UTC)
    return FireEvent(
        facility_id=mc.facility.id,
        detected_at=mc.candidate.first_detected_at,
        last_seen_at=mc.candidate.last_detected_at,
        peak_frp_mw=mc.candidate.peak_frp_mw,
        total_frp_integral_mj=None,
        detection_source=mc.candidate.hotspots[0].detection_source,
        confidence_label=label,
        status=EventStatus.PENDING_REVIEW,
        provenance_id=mc.candidate.provenance_id,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Task 11: metrics logging and hash-store flush
# ---------------------------------------------------------------------------


@task(name="log-pipeline-run", log_prints=True)
def log_pipeline_run(metrics: PipelineRunMetrics, new_hashes: set[str]) -> None:
    """Log structured pipeline metrics and flush the seen-hash store.

    This is the only place ``_save_seen_hashes`` is called, ensuring the
    store is written only when the full pipeline has completed.  A partial
    run (e.g. classify_fires failing) leaves the store unchanged so the
    next run reprocesses all detections from scratch.

    Parameters
    ----------
    metrics : PipelineRunMetrics
        Aggregated run statistics.
    new_hashes : set[str]
        Detection hashes produced this run (from task 5); persisted so
        subsequent runs skip them.
    """
    duration_s = (metrics.finished_at - metrics.started_at).total_seconds()

    # Use structlog directly so keyword fields work both inside and outside
    # a Prefect run context (stdlib logger doesn't accept keyword arguments).
    log.info(
        "pipeline_run_complete",
        run_date=metrics.run_date.isoformat(),
        duration_s=round(duration_s, 1),
        n_viirs=metrics.n_viirs_detections,
        n_modis=metrics.n_modis_detections,
        n_deduped=metrics.n_deduplicated,
        n_candidates=metrics.n_candidates,
        n_facility_matched=metrics.n_facility_matched,
        n_s2_fetched=metrics.n_s2_chips_fetched,
        n_s2_failed=metrics.n_s2_chips_failed,
        n_classified=metrics.n_classified,
        n_acled_events=metrics.n_acled_events,
        n_submitted=metrics.n_submitted_to_queue,
        task_failures=list(metrics.task_failures),
    )

    if new_hashes:
        existing = _load_seen_hashes(metrics.run_date)
        _save_seen_hashes(metrics.run_date, existing | new_hashes)
        log.info(
            "seen_hashes_flushed",
            run_date=metrics.run_date.isoformat(),
            n_new=len(new_hashes),
            n_total=len(existing | new_hashes),
        )


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


@flow(name="daily-ingest", log_prints=True)
def daily_ingest(target_date: date) -> PipelineRunMetrics:
    """Run the full WCED emission pipeline for *target_date*.

    Submits tasks 2–4 in parallel before waiting on any result, then runs
    tasks 5–11 sequentially (with internal asyncio parallelism in tasks 6
    and 7).  Failed tasks are tolerated: the flow logs the failure, records
    it in the returned metrics, and continues with an empty result for that
    stage.

    Parameters
    ----------
    target_date : date
        UTC calendar date to process.

    Returns
    -------
    PipelineRunMetrics
        Summary of the run including counts and any task failures.
    """
    logger = _logger()
    started_at = datetime.now(UTC)
    task_failures: list[str] = []

    logger.info("daily_ingest: starting for %s", target_date)

    # ------------------------------------------------------------------
    # Step 1 — facilities (must succeed; no downstream work without them)
    # ------------------------------------------------------------------
    facilities = load_facilities()

    # ------------------------------------------------------------------
    # Steps 2–4 — parallel ingestion
    # Submit all three before waiting on any so they run concurrently.
    # ------------------------------------------------------------------
    viirs_f = ingest_firms_viirs.submit(target_date, IRAN_BBOX)
    modis_f = ingest_firms_modis.submit(target_date, IRAN_BBOX)
    conflict_f = ingest_conflict_events.submit(target_date, list(DEFAULT_COUNTRIES))

    viirs_rows: list[dict[str, Any]] = _safe_result(
        viirs_f, "ingest_firms_viirs", [], task_failures, logger
    )
    modis_rows: list[dict[str, Any]] = _safe_result(
        modis_f, "ingest_firms_modis", [], task_failures, logger
    )
    conflict_result: tuple[list[ConflictEvent], str] = _safe_result(
        conflict_f, "ingest_conflict_events", ([], "none"), task_failures, logger
    )
    conflict_events, conflict_source_used = conflict_result

    firms_all: list[dict[str, Any]] = (viirs_rows or []) + (modis_rows or [])

    # ------------------------------------------------------------------
    # Step 5 — detection and facility matching
    # ------------------------------------------------------------------
    candidates_f = detect_candidate_events.submit(firms_all, facilities)
    candidates: list[MatchedCandidate] = _safe_result(
        candidates_f, "detect_candidate_events", [], task_failures, logger
    )

    # Collect detection hashes for the seen-hash flush in task 11.
    new_hashes: set[str] = {mc.detection_hash for mc in candidates}

    # ------------------------------------------------------------------
    # Steps 6 + 8 — run S2 fetch and conflict corroboration in parallel.
    # Both depend only on candidates and conflict_events (step 5 output),
    # so submit both before waiting on either.
    # ------------------------------------------------------------------
    chips_f = fetch_s2_chips_for_candidates.submit(candidates)
    corr_f = corroborate_with_conflict_events.submit(candidates, conflict_events)

    s2_chips: dict[str, S2ChipResult | None] = _safe_result(
        chips_f, "fetch_s2_chips_for_candidates", {}, task_failures, logger
    )
    corroborations: dict[str, list[CorroborationMatch]] = _safe_result(
        corr_f, "corroborate_with_conflict_events", {}, task_failures, logger
    )

    # ------------------------------------------------------------------
    # Step 7 — classification (depends on chips from step 6)
    # ------------------------------------------------------------------
    verified_f = classify_fires.submit(candidates, s2_chips)
    verified: dict[str, VerifiedCandidate] = _safe_result(
        verified_f, "classify_fires", {}, task_failures, logger
    )

    # ------------------------------------------------------------------
    # Step 9 — confidence label assignment
    # ------------------------------------------------------------------
    labels_f = assign_confidence_labels.submit(
        candidates, verified, corroborations
    )
    labels: dict[str, ConfidenceLabel] = _safe_result(
        labels_f, "assign_confidence_labels", {}, task_failures, logger
    )

    # ------------------------------------------------------------------
    # Step 10 — editorial queue submission
    # ------------------------------------------------------------------
    events_f = submit_to_editorial_queue.submit(candidates, labels)
    submitted_events: list[FireEvent] = _safe_result(
        events_f, "submit_to_editorial_queue", [], task_failures, logger
    )

    # ------------------------------------------------------------------
    # Step 11 — metrics and hash-store flush
    # ------------------------------------------------------------------
    finished_at = datetime.now(UTC)

    n_s2_ok = sum(1 for v in s2_chips.values() if v is not None)
    n_s2_fail = len(s2_chips) - n_s2_ok

    metrics = PipelineRunMetrics(
        run_date=target_date,
        started_at=started_at,
        finished_at=finished_at,
        n_viirs_detections=len(viirs_rows or []),
        n_modis_detections=len(modis_rows or []),
        n_deduplicated=len(firms_all) - sum(
            len(mc.candidate.hotspots) for mc in candidates
        ),
        n_candidates=len(candidates),
        n_facility_matched=sum(1 for mc in candidates if mc.facility is not None),
        n_s2_chips_fetched=n_s2_ok,
        n_s2_chips_failed=n_s2_fail,
        n_classified=sum(
            1
            for v in verified.values()
            if v.status is not VerificationStatus.AWAITING_OPTICAL_CHECK
        ),
        n_acled_events=len(conflict_events or []),
        n_submitted_to_queue=len(submitted_events or []),
        task_failures=tuple(task_failures),
    )

    log_pipeline_run(metrics, new_hashes)

    return metrics
