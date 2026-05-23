"""Export a static snapshot of the WCED API for the Vercel-hosted frontend.

Hits a running local API (default http://localhost:8000) and writes the JSON
responses into frontend/public/api-snapshot/. The frontend reads these files
directly when NEXT_PUBLIC_USE_SNAPSHOT=true (or NEXT_PUBLIC_API_URL is unset),
which lets the dashboard stay live on Vercel even when the backend is offline.

Usage:
    python scripts/export_snapshot.py
    python scripts/export_snapshot.py --api-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


REQUEST_DELAY_S = 1.1  # API limits to 60 req/min


REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_ROOT = REPO_ROOT / "frontend" / "public" / "api-snapshot"


def write_json(rel_path: str, data: Any) -> None:
    out = SNAPSHOT_ROOT / rel_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, default=str))
    print(f"  wrote {rel_path}")


def fetch(client: httpx.Client, path: str) -> Any:
    for attempt in range(6):
        r = client.get(path)
        if r.status_code == 429:
            wait = 2 ** attempt
            print(f"  429 on {path}, sleeping {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        time.sleep(REQUEST_DELAY_S)
        return r.json()
    r.raise_for_status()
    return r.json()


def try_fetch(client: httpx.Client, path: str) -> Any | None:
    try:
        return fetch(client, path)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-url", default="http://localhost:8000")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Exporting snapshot from {args.api_url} -> {SNAPSHOT_ROOT}")

    with httpx.Client(base_url=args.api_url, timeout=args.timeout) as c:
        def maybe(rel: str, path: str) -> None:
            data = try_fetch(c, path)
            if data is not None:
                write_json(rel, data)
            else:
                print(f"  skip {rel} (404)")

        maybe("meta.json", "/v1/meta")
        maybe("aggregates/headline.json", "/v1/aggregates/headline")
        maybe("timeseries/cumulative.json", "/v1/timeseries/cumulative")
        maybe("methodology/current.json", "/v1/methodology/current")
        maybe("changelog.json", "/v1/changelog")

        events = fetch(c, "/v1/events?status=PUBLISHED&per_page=200")
        facilities = fetch(c, "/v1/facilities?per_page=200")
        write_json("events.json", events)
        write_json("facilities.json", facilities)

        for ev in events.get("data", []):
            eid = ev["id"]
            write_json(f"events/{eid}.json", fetch(c, f"/v1/events/{eid}"))
            prov = try_fetch(c, f"/v1/events/{eid}/provenance")
            if prov is not None:
                write_json(f"events/{eid}/provenance.json", prov)
            assess = try_fetch(c, f"/v1/events/{eid}/assessment")
            if assess is not None:
                write_json(f"events/{eid}/assessment.json", assess)
            acled = try_fetch(c, f"/v1/events/{eid}/acled")
            if acled is not None:
                write_json(f"events/{eid}/acled.json", acled)

        for f in facilities.get("data", []):
            fid = f["id"]
            write_json(f"facilities/{fid}.json", fetch(c, f"/v1/facilities/{fid}"))

    write_json(
        "snapshot.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_api": args.api_url,
            "event_count": len(events.get("data", [])),
            "facility_count": len(facilities.get("data", [])),
        },
    )
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
