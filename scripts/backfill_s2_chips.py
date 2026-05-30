"""Backfill Sentinel-2 before/after chips for the top N events by p50.

Usage:
    python scripts/backfill_s2_chips.py [--top N] [--yes]

Fetches Sentinel-2 L2A chips from Microsoft Planetary Computer within ±72h of
each event's detected_at, renders true-color PNGs, and inserts rows into the
s2_chips table so the frontend can display before/after sliders.
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# Ensure project root is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from wced.db import models
from wced.db.repositories.ingestion import S2ChipRepository
from wced.db.session import get_engine, get_session_factory
from wced.ingest.sentinel2 import Sentinel2Connector

CHIP_DIR = Path(__file__).resolve().parent.parent / "data" / "s2_chips"
SEARCH_WINDOW_HOURS = 168  # 7 days — wider window for better scene availability
MAX_CLOUD_PCT = 30.0
CHIP_DELTA_DEG = 0.02  # ±0.02° ≈ 2 km around facility centroid


def _render_true_color_png(ds, path: Path) -> None:
    """Render an xarray Dataset with B04/B03/B02 bands to a PNG file."""
    from PIL import Image

    r = ds["B04"].values
    g = ds["B03"].values
    b = ds["B02"].values

    # Clip and scale to 0-255 with contrast stretch.
    def stretch(band: np.ndarray) -> np.ndarray:
        lo, hi = np.nanpercentile(band, [2, 98])
        if hi <= lo:
            hi = lo + 1e-6
        clipped = np.clip((band - lo) / (hi - lo), 0, 1)
        return (clipped * 255).astype(np.uint8)

    rgb = np.stack([stretch(r), stretch(g), stretch(b)], axis=-1)
    # Replace NaN-sourced pixels with black.
    nan_mask = np.isnan(r) | np.isnan(g) | np.isnan(b)
    rgb[nan_mask] = 0

    img = Image.fromarray(rgb)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), format="PNG")
    log.info("Saved PNG: %s (%dx%d)", path, img.width, img.height)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill S2 chips for top events")
    parser.add_argument("--top", type=int, default=5, help="Number of top events")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    engine = get_engine()
    Session = get_session_factory(engine)

    with Session() as session:
        # Find top N events by p50 (latest methodology version).
        ee = models.emission_estimates
        fe = models.fire_events
        fa = models.facilities

        # Subquery: max p50 per event.
        max_p50 = (
            select(
                ee.c.event_id,
                func.max(ee.c.p50).label("max_p50"),
            )
            .group_by(ee.c.event_id)
            .subquery()
        )
        top_events = session.execute(
            select(
                fe.c.id.label("event_id"),
                fe.c.detected_at,
                fe.c.facility_id,
                fa.c.name.label("facility_name"),
                func.ST_AsText(fa.c.geometry).label("geom_wkt"),
                max_p50.c.max_p50.label("p50"),
            )
            .join(fa, fe.c.facility_id == fa.c.id)
            .join(max_p50, max_p50.c.event_id == fe.c.id)
            .where(fe.c.status == "PUBLISHED")
            .order_by(max_p50.c.max_p50.desc())
            .limit(args.top)
        ).all()

        if not top_events:
            log.error("No published events with estimates found.")
            return

        # De-duplicate by event_id (an event may have multiple estimates).
        seen: set[UUID] = set()
        events = []
        for row in top_events:
            r = row._asdict()
            if r["event_id"] not in seen:
                seen.add(r["event_id"])
                events.append(r)

        log.info("Top %d events for S2 backfill:", len(events))
        for ev in events:
            log.info(
                "  %s  %s  p50=%.0f tCO2e  %s",
                ev["event_id"],
                ev["detected_at"].strftime("%Y-%m-%d"),
                ev["p50"],
                ev["facility_name"],
            )

        if not args.yes:
            resp = input(f"\nFetch S2 chips for {len(events)} events? [y/N] ")
            if resp.strip().lower() != "y":
                print("Aborted.")
                return

        connector = Sentinel2Connector(cache_dir=CHIP_DIR / "cache")
        s2_repo = S2ChipRepository(session)
        total_inserted = 0

        for ev in events:
            event_id = ev["event_id"]
            detected_at = ev["detected_at"]
            facility_name = ev["facility_name"]

            # Parse centroid from WKT.
            import re
            point_match = re.search(r"POINT\s*\(([-\d.]+)\s+([-\d.]+)\)", ev["geom_wkt"])
            if point_match:
                lon, lat = float(point_match.group(1)), float(point_match.group(2))
            else:
                poly_match = re.search(r"POLYGON\s*\(\((.*?)\)\)", ev["geom_wkt"])
                if poly_match:
                    coords = poly_match.group(1).split(",")
                    lons = [float(c.strip().split()[0]) for c in coords]
                    lats = [float(c.strip().split()[1]) for c in coords]
                    lon, lat = sum(lons) / len(lons), sum(lats) / len(lats)
                else:
                    log.warning("Cannot parse geometry for event %s, skipping", event_id)
                    continue

            bbox = (lon - CHIP_DELTA_DEG, lat - CHIP_DELTA_DEG,
                    lon + CHIP_DELTA_DEG, lat + CHIP_DELTA_DEG)

            for phase, window in [
                ("before", (
                    detected_at - timedelta(hours=SEARCH_WINDOW_HOURS),
                    detected_at - timedelta(hours=1),
                )),
                ("after", (
                    detected_at + timedelta(hours=1),
                    detected_at + timedelta(hours=SEARCH_WINDOW_HOURS),
                )),
            ]:
                log.info(
                    "Searching S2 %s for %s (%s) window %s to %s",
                    phase, facility_name, event_id,
                    window[0].strftime("%Y-%m-%d"), window[1].strftime("%Y-%m-%d"),
                )
                try:
                    items = connector.search_around(
                        lat, lon, window, max_cloud_pct=MAX_CLOUD_PCT
                    )
                except Exception as exc:
                    log.warning("S2 search failed for %s %s: %s", event_id, phase, exc)
                    continue

                if not items:
                    log.warning("No S2 scenes found for %s %s", event_id, phase)
                    continue

                best_item = items[0]
                log.info(
                    "Best scene: %s (cloud=%.1f%%)",
                    best_item.id,
                    best_item.properties.get("eo:cloud_cover", -1),
                )

                try:
                    ds, source = connector.fetch_chip(best_item, bbox)
                except Exception as exc:
                    log.warning("Chip fetch failed for %s %s: %s", event_id, phase, exc)
                    continue

                # Render to PNG.
                png_filename = f"{event_id}_{phase}.png"
                png_path = CHIP_DIR / png_filename
                try:
                    _render_true_color_png(ds, png_path)
                except Exception as exc:
                    log.warning("PNG render failed for %s %s: %s", event_id, phase, exc)
                    continue

                acq_date = best_item.datetime
                if acq_date is None:
                    acq_date = window[0]

                cloud_cover = best_item.properties.get("eo:cloud_cover")

                chip_id = uuid4()
                s2_repo.insert({
                    "id": chip_id,
                    "event_id": event_id,
                    "facility_id": ev["facility_id"],
                    "product_id": best_item.id,
                    "acquisition_date": acq_date,
                    "cloud_cover_pct": cloud_cover,
                    "storage_path": str(png_path.resolve()),
                    "bands": ["B04", "B03", "B02", "B12"],
                    "fetched_at": datetime.now(tz=UTC),
                })
                total_inserted += 1

        session.commit()
        log.info("Done. Inserted %d S2 chip records.", total_inserted)


if __name__ == "__main__":
    main()
