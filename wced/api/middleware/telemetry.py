"""OpenTelemetry, Prometheus metrics, and Sentry integration for WCED.

Call ``setup_telemetry(app)`` from the FastAPI application factory to wire
up all three systems:

1. **OpenTelemetry** — auto-instruments FastAPI (spans for every request),
   propagates trace IDs into structlog context vars, and exposes a manual
   ``tracer`` for custom spans around pipeline tasks.

2. **Prometheus** — exposes ``/metrics`` via ``prometheus_client``.  All
   five project-specific counters/gauges/histograms are defined here as
   module-level singletons so any module can import and increment them.

3. **Sentry** — initialises the SDK when ``SENTRY_DSN`` is set.

Environment variables
---------------------
OTEL_EXPORTER_OTLP_ENDPOINT  — OTLP collector (default: http://localhost:4317)
OTEL_SERVICE_NAME            — service name (default: wced-api)
SENTRY_DSN                   — Sentry ingest URL (optional)
WCED_ENVIRONMENT             — passed to Sentry as ``environment``
"""
from __future__ import annotations

import os
import time
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from wced.logging import bind_context, clear_context

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics (always available, even if prometheus_client is missing)
# ---------------------------------------------------------------------------

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )

    REGISTRY = CollectorRegistry()

    INGEST_EVENTS_TOTAL = Counter(
        "wced_ingest_events_total",
        "Cumulative count of raw events ingested from external sources.",
        labelnames=["source"],
        registry=REGISTRY,
    )

    PIPELINE_DURATION_SECONDS = Histogram(
        "wced_pipeline_duration_seconds",
        "Wall-clock duration of pipeline flow runs.",
        labelnames=["flow"],
        buckets=(10, 30, 60, 120, 300, 600, 1800, 3600),
        registry=REGISTRY,
    )

    CLAUDE_TOKENS_TOTAL = Counter(
        "wced_claude_tokens_total",
        "Cumulative token usage for Anthropic Claude API calls.",
        labelnames=["purpose"],
        registry=REGISTRY,
    )

    ESTIMATES_DISTRIBUTION_WIDTH = Histogram(
        "wced_estimates_distribution_width_p95_minus_p5",
        "Width of emission estimate uncertainty interval (p95 - p5) in tCO2e.",
        labelnames=["event_type"],
        buckets=(10, 50, 100, 500, 1000, 5000, 10000, 50000),
        registry=REGISTRY,
    )

    EDITORIAL_QUEUE_DEPTH = Gauge(
        "wced_editorial_queue_depth",
        "Number of fire events currently in PENDING_REVIEW status.",
        registry=REGISTRY,
    )

    HTTP_REQUESTS_TOTAL = Counter(
        "wced_http_requests_total",
        "Total HTTP requests by method, path, and status.",
        labelnames=["method", "path", "status"],
        registry=REGISTRY,
    )

    HTTP_REQUEST_DURATION_SECONDS = Histogram(
        "wced_http_request_duration_seconds",
        "HTTP request latency.",
        labelnames=["method", "path"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        registry=REGISTRY,
    )

    _PROM_AVAILABLE = True

except ImportError:
    _PROM_AVAILABLE = False
    REGISTRY = None  # type: ignore[assignment]
    INGEST_EVENTS_TOTAL = None  # type: ignore[assignment]
    PIPELINE_DURATION_SECONDS = None  # type: ignore[assignment]
    CLAUDE_TOKENS_TOTAL = None  # type: ignore[assignment]
    ESTIMATES_DISTRIBUTION_WIDTH = None  # type: ignore[assignment]
    EDITORIAL_QUEUE_DEPTH = None  # type: ignore[assignment]
    HTTP_REQUESTS_TOTAL = None  # type: ignore[assignment]
    HTTP_REQUEST_DURATION_SECONDS = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------

_OTEL_AVAILABLE = False
tracer: Any = None  # module-level tracer for manual spans

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    _OTEL_AVAILABLE = True
except ImportError:
    pass


def _setup_otel(app: FastAPI) -> None:
    """Initialise OpenTelemetry tracing with OTLP export and FastAPI auto-instrumentation."""
    global tracer

    if not _OTEL_AVAILABLE:
        log.info("opentelemetry.skip", reason="opentelemetry packages not installed")
        return

    service_name = os.environ.get("OTEL_SERVICE_NAME", "wced-api")
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("wced")

    FastAPIInstrumentor.instrument_app(app)
    log.info("opentelemetry.configured", service=service_name, endpoint=otlp_endpoint)


# ---------------------------------------------------------------------------
# Sentry setup
# ---------------------------------------------------------------------------


def _setup_sentry() -> None:
    """Initialise Sentry error tracking when SENTRY_DSN is set."""
    dsn = os.environ.get("SENTRY_DSN", "")
    if not dsn:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("WCED_ENVIRONMENT", "development"),
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            profiles_sample_rate=float(os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
            integrations=[
                FastApiIntegration(),
                SqlalchemyIntegration(),
            ],
            send_default_pii=False,
        )
        log.info("sentry.configured", environment=os.environ.get("WCED_ENVIRONMENT"))
    except ImportError:
        log.info("sentry.skip", reason="sentry-sdk not installed")


# ---------------------------------------------------------------------------
# Request middleware — metrics + context binding
# ---------------------------------------------------------------------------


def _extract_trace_id() -> str | None:
    """Extract the current OTel trace ID if available."""
    if not _OTEL_AVAILABLE:
        return None
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        return format(ctx.trace_id, "032x")
    return None


class MetricsMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that records per-request Prometheus metrics and
    binds the OTel trace ID into the structlog context."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        trace_id = _extract_trace_id()
        bind_context(trace_id=trace_id)

        path = request.url.path
        method = request.method
        t0 = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            if _PROM_AVAILABLE:
                HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status="500").inc()
            clear_context()
            raise

        elapsed = time.perf_counter() - t0
        status = str(response.status_code)

        if _PROM_AVAILABLE:
            HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status=status).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(elapsed)

        if trace_id:
            response.headers["X-Trace-Id"] = trace_id

        clear_context()
        return response


# ---------------------------------------------------------------------------
# Prometheus /metrics endpoint
# ---------------------------------------------------------------------------


async def _metrics_endpoint(request: Request) -> Response:
    """Serve Prometheus metrics in text exposition format."""
    if not _PROM_AVAILABLE:
        return Response("prometheus_client not installed", status_code=501)
    body = generate_latest(REGISTRY)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Pipeline span helpers
# ---------------------------------------------------------------------------


def pipeline_span(flow_name: str) -> Any:
    """Return a context manager that creates an OTel span for a pipeline flow.

    Usage::

        with pipeline_span("daily-ingest") as span:
            span.set_attribute("target_date", "2026-03-15")
            ...
    """
    if tracer is not None:
        return tracer.start_as_current_span(
            f"pipeline.{flow_name}",
            attributes={"pipeline.flow": flow_name},
        )

    from contextlib import nullcontext

    return nullcontext()


def record_pipeline_duration(flow_name: str, duration_seconds: float) -> None:
    """Record a completed pipeline flow's duration in Prometheus."""
    if _PROM_AVAILABLE and PIPELINE_DURATION_SECONDS is not None:
        PIPELINE_DURATION_SECONDS.labels(flow=flow_name).observe(duration_seconds)


def record_ingest_count(source: str, count: int) -> None:
    """Increment the ingest event counter."""
    if _PROM_AVAILABLE and INGEST_EVENTS_TOTAL is not None:
        INGEST_EVENTS_TOTAL.labels(source=source).inc(count)


def record_claude_tokens(purpose: str, tokens: int) -> None:
    """Increment the Claude token counter."""
    if _PROM_AVAILABLE and CLAUDE_TOKENS_TOTAL is not None:
        CLAUDE_TOKENS_TOTAL.labels(purpose=purpose).inc(tokens)


def record_estimate_width(event_type: str, width: float) -> None:
    """Record an emission estimate's uncertainty width."""
    if _PROM_AVAILABLE and ESTIMATES_DISTRIBUTION_WIDTH is not None:
        ESTIMATES_DISTRIBUTION_WIDTH.labels(event_type=event_type).observe(width)


def set_editorial_queue_depth(depth: int) -> None:
    """Set the current editorial queue depth gauge."""
    if _PROM_AVAILABLE and EDITORIAL_QUEUE_DEPTH is not None:
        EDITORIAL_QUEUE_DEPTH.set(depth)


# ---------------------------------------------------------------------------
# Prefect trace ID propagation
# ---------------------------------------------------------------------------


def propagate_trace_to_prefect() -> dict[str, str]:
    """Extract current OTel context as a dict suitable for passing to
    Prefect flow parameters, so child flows can re-attach to the same trace."""
    if not _OTEL_AVAILABLE:
        return {}
    from opentelemetry.context import get_current
    from opentelemetry.propagate import inject

    carrier: dict[str, str] = {}
    inject(carrier, context=get_current())
    return carrier


def attach_trace_from_prefect(carrier: dict[str, str]) -> None:
    """Re-attach an OTel trace context propagated from a parent Prefect flow."""
    if not _OTEL_AVAILABLE or not carrier:
        return
    from opentelemetry.context import attach
    from opentelemetry.propagate import extract

    ctx = extract(carrier)
    attach(ctx)


# ---------------------------------------------------------------------------
# Top-level setup
# ---------------------------------------------------------------------------


def setup_telemetry(app: FastAPI) -> None:
    """Wire up OpenTelemetry, Prometheus, and Sentry on *app*.

    Call this once from the application factory.
    """
    from wced.logging import configure_logging

    configure_logging()

    _setup_sentry()
    _setup_otel(app)

    app.add_middleware(MetricsMiddleware)
    app.add_route("/metrics", _metrics_endpoint, methods=["GET"])

    log.info("telemetry.setup_complete")
