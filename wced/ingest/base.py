"""Ingest connector protocol.

Every external data source (FIRMS, Sentinel-2 via STAC, ACLED, news scrapers,
etc.) is wrapped in an ``IngestConnector``. Connectors yield *raw* records as
plain dicts; mapping those dicts onto domain models (``Facility``,
``FireEvent``) is the responsibility of downstream modules in ``wced.detect``
and ``wced.verify``. This separation keeps connectors thin and reusable —
swapping a source does not force a change in detection logic.

A connector is also responsible for producing a ``Source`` record per
upstream API response (see ``wced.models.provenance.Source``). The Source is
yielded out-of-band via the per-record dict under the ``_source`` key so the
caller can persist it into the provenance store without re-fetching the
response body.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

# A bounding box in WGS84 (EPSG:4326): (west, south, east, north).
# Tuples chosen over a dataclass so callers can pass GeoJSON-derived bboxes
# without first wrapping them.
BBox = tuple[float, float, float, float]


@runtime_checkable
class IngestConnector(Protocol):
    """Interface implemented by every data-source ingest module.

    Connectors are async generators: they may interleave network I/O with
    yielding records, which matters for high-cadence sources like geostationary
    fire feeds. Implementations should be polite — honour upstream rate limits
    and back off on transient failures (5xx, network errors) rather than
    surfacing them to the caller.

    The yielded dict is source-shaped (it preserves the upstream field names)
    so that the connector itself contains no domain-specific mapping. Two
    reserved keys are added by the connector:

    - ``_source`` — a ``wced.models.provenance.Source`` instance for the API
      response this record came from. The same Source is attached to every
      record that came from a single response.
    - ``detected_at`` — a UTC ``datetime`` derived by the connector from
      whatever per-record timestamps the upstream provides. Centralising this
      here means downstream code can rely on a single timezone-aware field
      regardless of source.
    """

    @property
    def name(self) -> str:
        """Short identifier used in logs and provenance, e.g. ``"firms_viirs"``."""
        ...

    def ingest(
        self,
        start: datetime,
        end: datetime,
        bbox: BBox,
    ) -> AsyncIterator[dict]:
        """Yield raw records observed within [start, end] inside ``bbox``.

        Parameters
        ----------
        start, end : datetime
            Inclusive time window (UTC). Naive datetimes are rejected by
            implementations; callers must pass timezone-aware values.
        bbox : BBox
            ``(west, south, east, north)`` in WGS84 (EPSG:4326).

        Yields
        ------
        dict
            One record per upstream observation. Field names mirror the
            upstream API; ``_source`` and ``detected_at`` are added by the
            connector (see class docstring).
        """
        ...
