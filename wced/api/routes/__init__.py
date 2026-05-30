"""Route modules for the WCED API."""
from wced.api.routes.aggregates import router as aggregates_router
from wced.api.routes.events import router as events_router
from wced.api.routes.facilities import router as facilities_router
from wced.api.routes.meta import router as meta_router
from wced.api.routes.timeseries import router as timeseries_router

__all__ = [
    "aggregates_router",
    "events_router",
    "facilities_router",
    "meta_router",
    "timeseries_router",
]
