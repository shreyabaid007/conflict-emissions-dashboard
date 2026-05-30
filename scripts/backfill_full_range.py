#!/usr/bin/env python3
"""Backfill FIRMS ingest + detect + quantify for the full conflict period.

Resumable: checks firms_detections for each date before ingesting.
Runs detect in weekly windows to avoid OOM on the pairwise distance matrix.

Run from the repo root:
    python scripts/backfill_full_range.py
"""

import subprocess
import sys
from datetime import date, timedelta

COMPOSE = ["docker", "compose", "-f", "deploy/docker-compose.yml"]
START_DATE = date(2026, 2, 28)
END_DATE = date(2026, 5, 24)
DETECT_WINDOW_DAYS = 7


def run(cmd: list[str], *, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd[-6:])}", flush=True)
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


def date_already_ingested(d: date) -> bool:
    r = run(
        [*COMPOSE, "exec", "postgres", "psql", "-U", "wced", "-tA", "-c",
         f"SELECT COUNT(*) FROM firms_detections WHERE acq_datetime::date = '{d.isoformat()}';"],
        check=False,
    )
    try:
        return int(r.stdout.strip()) > 0
    except ValueError:
        return False


def ingest_date(d: date) -> None:
    r = run(
        [*COMPOSE, "exec", "wced-api", "wced", "ingest", "firms", "--date", d.isoformat(), "--yes"],
        check=False, timeout=120,
    )
    if r.returncode != 0:
        print(f"    WARN ingest failed: {r.stderr.strip()[:200]}", flush=True)
    else:
        last_line = [l for l in r.stdout.strip().splitlines() if l][-1] if r.stdout.strip() else ""
        print(f"    {last_line}", flush=True)


def detect_window(since: date, until: date) -> None:
    cmd = [
        *COMPOSE, "exec", "wced-api", "wced", "detect",
        "--since", since.isoformat(),
        "--until", until.isoformat(),
        "--yes",
    ]
    r = run(cmd, check=False, timeout=300)
    last_line = r.stdout.strip().split("\n")[-1] if r.stdout.strip() else f"stderr: {r.stderr.strip()[:200]}"
    print(f"    {last_line}", flush=True)


def main() -> None:
    dates = []
    d = START_DATE
    while d <= END_DATE:
        dates.append(d)
        d += timedelta(days=1)

    print(f"=== Backfill: {len(dates)} dates from {START_DATE} to {END_DATE} ===\n", flush=True)

    # --- Phase 1: ingest FIRMS per date ---
    ingested, skipped = 0, 0
    for i, d in enumerate(dates, 1):
        tag = f"[{i}/{len(dates)}] {d.isoformat()}"
        if date_already_ingested(d):
            skipped += 1
            if i % 10 == 0 or i == len(dates):
                print(f"{tag} — already ingested, skipping", flush=True)
            continue
        print(f"{tag} — ingesting...", flush=True)
        ingest_date(d)
        ingested += 1

    print(f"\n=== Ingest complete: {ingested} new, {skipped} skipped ===\n", flush=True)

    # --- Phase 2: detect in weekly windows ---
    print(f"=== Running detect in {DETECT_WINDOW_DAYS}-day windows ===", flush=True)
    window_start = START_DATE
    window_num = 0
    while window_start <= END_DATE:
        window_end = min(window_start + timedelta(days=DETECT_WINDOW_DAYS), END_DATE + timedelta(days=1))
        window_num += 1
        print(f"  Window {window_num}: {window_start} → {window_end}", flush=True)
        detect_window(window_start, window_end)
        window_start = window_end
    print(f"  {window_num} windows processed.", flush=True)

    # --- Phase 3: quantify ---
    print("\n=== Running quantify ===", flush=True)
    r = run(
        [*COMPOSE, "exec", "wced-api", "wced", "quantify", "--all-published", "--yes"],
        check=False, timeout=600,
    )
    print(r.stdout.strip().split("\n")[-1] if r.stdout.strip() else f"quantify stderr: {r.stderr.strip()[:300]}", flush=True)

    # --- Phase 4: dedup fire_events ---
    print("\n=== Deduplicating fire_events ===", flush=True)
    dedup_fire_sql = """
    WITH ranked AS (
      SELECT id,
             ROW_NUMBER() OVER (
               PARTITION BY facility_id, detected_at, last_seen_at
               ORDER BY total_frp_integral_mj DESC NULLS LAST,
                        peak_frp_mw DESC,
                        created_at ASC
             ) AS rn
      FROM fire_events
    ),
    to_delete AS (
      SELECT id FROM ranked WHERE rn > 1
    )
    DELETE FROM emission_estimates
    WHERE event_id IN (SELECT id FROM to_delete);
    """
    r = run([*COMPOSE, "exec", "postgres", "psql", "-U", "wced", "-c", dedup_fire_sql], check=False)
    ee_deleted = r.stdout.strip().split("\n")[-1] if r.stdout.strip() else "?"
    print(f"  emission_estimates orphans deleted: {ee_deleted}", flush=True)

    dedup_fire_sql2 = """
    WITH ranked AS (
      SELECT id,
             ROW_NUMBER() OVER (
               PARTITION BY facility_id, detected_at, last_seen_at
               ORDER BY total_frp_integral_mj DESC NULLS LAST,
                        peak_frp_mw DESC,
                        created_at ASC
             ) AS rn
      FROM fire_events
    )
    DELETE FROM fire_events
    WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
    """
    r = run([*COMPOSE, "exec", "postgres", "psql", "-U", "wced", "-c", dedup_fire_sql2], check=False)
    fe_deleted = r.stdout.strip().split("\n")[-1] if r.stdout.strip() else "?"
    print(f"  fire_events duplicates deleted: {fe_deleted}", flush=True)

    # Dedup emission_estimates (keep latest per event)
    dedup_ee_sql = """
    WITH ranked AS (
      SELECT id,
             ROW_NUMBER() OVER (
               PARTITION BY event_id
               ORDER BY created_at DESC
             ) AS rn
      FROM emission_estimates
    )
    DELETE FROM emission_estimates
    WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
    """
    r = run([*COMPOSE, "exec", "postgres", "psql", "-U", "wced", "-c", dedup_ee_sql], check=False)
    ee_dedup = r.stdout.strip().split("\n")[-1] if r.stdout.strip() else "?"
    print(f"  emission_estimates duplicates deleted: {ee_dedup}", flush=True)

    # --- Phase 5: report ---
    print("\n=== Final report ===\n", flush=True)
    report_sql = r"""
    SELECT '--- Summary ---' AS label;

    SELECT COUNT(DISTINCT acq_datetime::date) AS dates_ingested FROM firms_detections;
    SELECT COUNT(*) AS total_fire_events FROM fire_events;
    SELECT COUNT(*) AS events_with_estimates FROM emission_estimates;

    SELECT
      ROUND(SUM(CASE WHEN f.country='IRN' THEN ee.p50 ELSE 0 END)::numeric, 1) AS irn_p50,
      ROUND(SUM(ee.p50)::numeric, 1) AS all_p50
    FROM emission_estimates ee
    JOIN fire_events e ON ee.event_id = e.id
    JOIN facilities f ON e.facility_id = f.id;

    SELECT '--- Top 5 events by p50 ---' AS label;
    SELECT f.name, e.detected_at::date AS dt,
           ROUND(ee.p50::numeric,1) AS p50,
           ROUND(ee.p5::numeric,1) AS p5,
           ROUND(ee.p95::numeric,1) AS p95
    FROM emission_estimates ee
    JOIN fire_events e ON ee.event_id = e.id
    JOIN facilities f ON e.facility_id = f.id
    ORDER BY ee.p50 DESC LIMIT 5;

    SELECT '--- Highest single-day p50 ---' AS label;
    SELECT e.detected_at::date AS dt,
           ROUND(SUM(ee.p50)::numeric,1) AS day_p50
    FROM emission_estimates ee
    JOIN fire_events e ON ee.event_id = e.id
    GROUP BY e.detected_at::date
    ORDER BY day_p50 DESC LIMIT 1;
    """
    r = run([*COMPOSE, "exec", "postgres", "psql", "-U", "wced", "-c", report_sql], check=False)
    print(r.stdout, flush=True)

    print("=== Done ===", flush=True)


if __name__ == "__main__":
    main()
