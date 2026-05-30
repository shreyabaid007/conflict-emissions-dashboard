"""WCED public API — FastAPI application factory and entrypoint."""
from __future__ import annotations

from fastapi import Depends, FastAPI

from wced.api.dependencies import rate_limit
from wced.api.middleware.telemetry import setup_telemetry
from wced.api.routes import (
    aggregates_router,
    events_router,
    facilities_router,
    meta_router,
    timeseries_router,
)

DESCRIPTION = """\
Public, read-only API for the War Carbon Emissions Dashboard (WCED).

All emission estimates are probability distributions (p5 / p50 / p95),
never point values. Every number traces to a provenance chain of cited
sources. Data is CC-BY 4.0; code is MIT.

**Rate limits:** Anonymous requests are limited to 60/min per IP.
Provide an `X-API-Key` header for higher limits.
"""


def create_app() -> FastAPI:
    """Application factory — returns a configured FastAPI instance."""
    app = FastAPI(
        title="War Carbon Emissions Dashboard",
        description=DESCRIPTION,
        version="0.1.0",
        license_info={"name": "MIT"},
        docs_url="/docs",
        redoc_url="/redoc",
        dependencies=[Depends(rate_limit)],
    )

    setup_telemetry(app)

    app.include_router(events_router)
    app.include_router(facilities_router)
    app.include_router(timeseries_router)
    app.include_router(aggregates_router)
    app.include_router(meta_router)

    return app


app = create_app()
