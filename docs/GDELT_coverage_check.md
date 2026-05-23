# GDELT Coverage Check — does GDELT corroborate our conflict window?

**Date:** 2026-05-30
**Type:** Diagnostic only. Live read-only calls to the public GDELT DOC 2.0 API.
No DB writes, no schema changes. Local DB used only to read event dates/coords.

## Question

Before building the DB-backed verification rework, prove GDELT actually returns
corroboration for our window — pick real high-emission events, call the GDELT
connector live for each event's date ±window and facility AOI, and decide yes/no on
usable coverage.

## Events tested (top non-zero v1.1.0 emitters, from local DB)

| Facility | detected_at → last_seen_at | lat, lon | p50 (tCO₂e) |
|---|---|---|---|
| Ahvaz / Karoon production area | 2026-02-28 → 2026-03-06 | 31.005, 48.143 | 22,703.6 |
| Ahvaz / Karoon production area | 2026-03-07 → 2026-03-11 | 31.005, 48.143 | 19,786.5 |
| South Pars / Asaluyeh gas complex | 2026-03-17 → 2026-03-19 | 27.506, 52.634 | 7,727.8 |
| Tehran Refinery (south tank farm) | 2026-03-08 → 2026-03-09 | 35.542, 51.430 | 3,310.7 |

## What the live calls showed

### 1. GDELT *has* data for the conflict window (coverage exists)
A **wide** date-windowed DOC query over the conflict theatre
(`startdatetime=20260228`, `enddatetime=20260530`,
`query="(Iran OR Tehran OR Ahvaz) (strike OR fire OR attack OR explosion)"`)
returned **50 articles** with `seendate` spanning **2026-03-04 … 2026-05-26**. A
no-date control query returned fresh articles (e.g. `seendate 20260512`). So the
underlying news coverage for the war period is present in GDELT and the DOC API does
accept these 2026 dates.

### 2. …but the connector extracts **zero geolocated events** (decisive blocker)
The DOC 2.0 **ArtList** response carries only article-level metadata. A populated
article's full field set is:

```
['domain', 'language', 'seendate', 'socialimage', 'sourcecountry', 'title', 'url', 'url_mobile']
```

**There are no latitude/longitude fields** — only `sourcecountry` (a country-name
string). But `wced/ingest/gdelt.py::_parse_doc_article` (lines 150-195) reads:

```python
lat = float(article.get("sourcecountylat") or ...)   # field does not exist → 0.0
lon = float(article.get("sourcecountylon") or 0)      # field does not exist → 0.0
if lat == 0.0 and lon == 0.0:
    return None
```

Those keys (`sourcecountylat`/`sourcecountylon`) **are not present in the DOC API
ArtList response**, so `lat=lon=0` and the parser returns `None` for **every**
article. Across all connector runs, `parsed_geo` (non-None `GDELTEvent`s) was **0**,
even when raw articles were returned. Since `find_corroboration`
(`wced/verify/corroboration.py`) matches on haversine distance to the facility, it
can never match an event that has no coordinates. **Result: GDELT corroboration via
`query_events_api` is structurally impossible as currently built — not a data gap, a
field-mapping gap.**

### 3. Per-event narrow windows return little/nothing, and the API is inconsistent
Per-event ±1-day windows (`query_events_api` with `DEFAULT_DOC_QUERY`) returned **0
articles** for every event tested. Only a wide multi-month window returned data, and
even that varied run-to-run (the same wide window returned 50 articles via one query
and 0 via a slightly different query minutes later). Per-event date-scoped historical
DOC queries are therefore unreliable even setting aside the coordinate problem.

### 4. Aggressive rate limiting; the connector crashes on it
GDELT enforces **1 request / 5 seconds** and applies a cumulative per-IP cooldown —
we hit `HTTP 429 "Please limit requests to one every 5 seconds"` repeatedly even at
8-second spacing. The connector only retries **5xx** (`_get_with_retry`, gdelt.py:519)
and raises `GDELTError` on 429 (≥400), so any throttle **aborts** the run. Backfilling
67 events × date-windowed queries would trip this constantly.

### 5. The geo-aware path (`fetch_latest_events`) can't reach history
`fetch_latest_events` / `_parse_csv_row` **does** read real coordinates
(`ActionGeo_Lat`/`ActionGeo_Long`) — but it only downloads the **latest 15-minute**
export discovered from `lastupdate.txt` (i.e. "now"). It cannot fetch March 2026.
Historical Events 2.0 data requires the **dated flat-file archive**
(`data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip`) or **BigQuery** —
neither is implemented in the connector.

## Conclusion

**Does GDELT have usable coverage for this conflict window?**

- **Raw news coverage: YES.** Conflict-theatre articles exist across 2026-03 → 2026-05
  and are retrievable from the DOC API.
- **Usable *corroboration* via the current connector: NO.** The DOC `query_events_api`
  path yields **zero geolocated events** because DOC ArtList responses contain no
  coordinates and the parser looks for fields that don't exist — so spatial matching
  to facilities is impossible. Add unreliable narrow-window results and 429
  fragility, and the current path cannot corroborate any event.

**Implication for the rework:** Do **not** wire the DB-backed verification backfill
onto `query_events_api` as-is — it would persist zero corroboration. The GDELT source
must first be switched to a **geocoded** one. Options, in order of fit:

1. **GDELT GEO 2.0 API** (`api.gdeltproject.org/api/v2/geo/geo`) — returns geocoded
   points (lat/lon) for a query+timespan; closest drop-in for spatial corroboration.
2. **Events 2.0 historical flat-files** — dated `*.export.CSV.zip` archives, parsed by
   the existing `_parse_csv_row` (already reads `ActionGeo_Lat/Long`); add a
   date-ranged fetch instead of `lastupdate.txt`-only.
3. **GDELT BigQuery** (`gdelt-bq.events`) — full history with coordinates; heaviest
   but most complete for a one-time backfill of 67 events.

Whichever source is chosen also needs: per-event caching, ≥5 s pacing with **429
retry/backoff** (the connector currently lacks 429 retry), and a `gdelt_events` /
`conflict_events` persistence table (see `docs/DIAGNOSIS_corroboration_gap.md`).

Until then, the honest position stands: the v1.1.0 GDELT promotion has correct code
but **no usable corroboration input**, and the headline will remain `REPORTED` after
any DB-backed rework that still relies on `query_events_api`.

---

*All findings from live read-only GDELT API calls and source review. The local DB was
read only for event coordinates/dates; no schema or data modified.*
