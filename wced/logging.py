"""Structured logging configuration for WCED.

Call ``configure_logging()`` once at process startup (API entrypoint, CLI,
Prefect worker).  All subsequent ``structlog.get_logger()`` calls inherit
the configuration.

Environment controls:
  WCED_ENVIRONMENT  — "production" → JSON to stdout; anything else → coloured
                      console output.
  WCED_LOG_LEVEL    — stdlib level name (default: INFO).

Context variables (``event_id``, ``facility_id``, ``methodology_version``)
are propagated via ``contextvars`` so they survive across async boundaries
and appear in every log record emitted within the same request or task.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextvars import ContextVar
from typing import Any
from uuid import UUID

import structlog

# ---------------------------------------------------------------------------
# Context variables — bind once per request/task, read by the processor
# ---------------------------------------------------------------------------

ctx_event_id: ContextVar[str | None] = ContextVar("ctx_event_id", default=None)
ctx_facility_id: ContextVar[str | None] = ContextVar("ctx_facility_id", default=None)
ctx_methodology_version: ContextVar[str | None] = ContextVar(
    "ctx_methodology_version", default=None
)
ctx_trace_id: ContextVar[str | None] = ContextVar("ctx_trace_id", default=None)


def bind_context(
    *,
    event_id: str | UUID | None = None,
    facility_id: str | UUID | None = None,
    methodology_version: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Set context variables that will appear in every subsequent log line."""
    if event_id is not None:
        ctx_event_id.set(str(event_id))
    if facility_id is not None:
        ctx_facility_id.set(str(facility_id))
    if methodology_version is not None:
        ctx_methodology_version.set(methodology_version)
    if trace_id is not None:
        ctx_trace_id.set(trace_id)


def clear_context() -> None:
    """Reset all context variables (call at end of request/task)."""
    ctx_event_id.set(None)
    ctx_facility_id.set(None)
    ctx_methodology_version.set(None)
    ctx_trace_id.set(None)


def _inject_context(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Structlog processor that injects context variables into every record."""
    for name, var in (
        ("event_id", ctx_event_id),
        ("facility_id", ctx_facility_id),
        ("methodology_version", ctx_methodology_version),
        ("trace_id", ctx_trace_id),
    ):
        val = var.get()
        if val is not None:
            event_dict.setdefault(name, val)
    return event_dict


# ---------------------------------------------------------------------------
# External API call logging helper
# ---------------------------------------------------------------------------


class ExternalCallLogger:
    """Context manager that logs latency, response size, and status for
    external HTTP calls."""

    def __init__(self, service: str, endpoint: str, method: str = "GET") -> None:
        self._log = structlog.get_logger("wced.external")
        self._service = service
        self._endpoint = endpoint
        self._method = method
        self._t0: float = 0.0
        self.status_code: int | None = None
        self.response_bytes: int | None = None

    def __enter__(self) -> ExternalCallLogger:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        latency_ms = (time.perf_counter() - self._t0) * 1000.0
        log_kw: dict[str, Any] = {
            "service": self._service,
            "endpoint": self._endpoint,
            "method": self._method,
            "latency_ms": round(latency_ms, 1),
        }
        if self.status_code is not None:
            log_kw["status"] = self.status_code
        if self.response_bytes is not None:
            log_kw["response_bytes"] = self.response_bytes

        if exc_type is not None:
            log_kw["error"] = str(exc_val)
            self._log.error("external_call_failed", **log_kw)
        else:
            self._log.info("external_call", **log_kw)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def configure_logging(
    *,
    env: str | None = None,
    level: str | None = None,
) -> None:
    """Configure structlog and stdlib logging for the process.

    Parameters
    ----------
    env : str or None
        Override ``WCED_ENVIRONMENT``.  ``"production"`` → JSON renderer;
        anything else → coloured console renderer.
    level : str or None
        Override ``WCED_LOG_LEVEL``.  Default ``"INFO"``.
    """
    env = env or os.environ.get("WCED_ENVIRONMENT", "development")
    level = level or os.environ.get("WCED_LOG_LEVEL", "INFO")
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if env == "production":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for noisy in ("httpx", "httpcore", "urllib3", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
