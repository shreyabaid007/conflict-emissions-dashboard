"""FastAPI dependency callables — DB session, authentication, rate limiting."""
from __future__ import annotations

import os
import time
from collections import defaultdict
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from wced.db.session import get_engine, get_session_factory

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session scoped to one request."""
    factory = get_session_factory(get_engine())
    session = factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


def _valid_api_keys() -> set[str]:
    raw = os.environ.get("WCED_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def require_api_key(
    api_key: str | None = Security(_api_key_header),
) -> str:
    """Raise 401 if the request lacks a valid API key."""
    valid = _valid_api_keys()
    if not valid:
        raise HTTPException(503, detail="API key authentication not configured")
    if api_key is None or api_key not in valid:
        raise HTTPException(401, detail="Invalid or missing API key")
    return api_key


_ANON_LIMIT = 60
_WINDOW_SECONDS = 60
_buckets: dict[str, list[float]] = defaultdict(list)


def rate_limit(request: Request) -> None:
    """Enforce per-IP anonymous rate limit (60 req/min).

    Requests carrying a valid API key bypass the limit.
    """
    key_header = request.headers.get("X-API-Key")
    if key_header and key_header in _valid_api_keys():
        return

    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = _buckets[ip]
    window[:] = [t for t in window if now - t < _WINDOW_SECONDS]
    if len(window) >= _ANON_LIMIT:
        raise HTTPException(429, detail="Rate limit exceeded (60 requests/minute)")
    window.append(now)
