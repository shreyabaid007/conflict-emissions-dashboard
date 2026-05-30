"""Modal deployment for WCED — serverless API + scheduled pipeline crons.

Exposes:
  - FastAPI app via @modal.asgi_app() (scale-to-zero)
  - Daily ingest cron at 06:00 UTC
  - Weekly validation/literature batch at 02:00 UTC Mondays
"""
from __future__ import annotations

import modal

app = modal.App("wced")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("gdal-bin", "libgdal-dev", "libproj-dev", "libgeos-dev")
    .pip_install(
        "psycopg2-binary>=2.9",
        "uvicorn[standard]>=0.29",
    )
    .add_local_dir(".", remote_path="/root/wced", copy=True, ignore=[
        ".venv", "node_modules", "__pycache__", ".git", "frontend",
    ])
    .run_commands("pip install '/root/wced[api]'")
)

secrets = modal.Secret.from_name("wced-secrets")


@app.function(
    image=image,
    secrets=[secrets],
    scaledown_window=300,
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def api():
    from wced.api.main import create_app

    return create_app()


@app.function(
    image=image,
    secrets=[secrets],
    timeout=1800,
    retries=modal.Retries(max_retries=2, backoff_coefficient=2.0),
    schedule=modal.Cron("0 6 * * *"),
)
def daily_ingest_cron():
    """Run the full daily ingest + quantification pipeline."""
    import logging
    from datetime import date, timedelta

    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    log = structlog.get_logger("modal.daily_ingest")

    target = date.today() - timedelta(days=1)
    log.info("daily_ingest_cron.start", target_date=target.isoformat())

    from wced.pipeline.daily_ingest import daily_ingest

    metrics = daily_ingest(target)
    log.info(
        "daily_ingest_cron.complete",
        target_date=target.isoformat(),
        n_candidates=metrics.n_candidates,
        n_submitted=metrics.n_submitted_to_queue,
        task_failures=list(metrics.task_failures),
    )


@app.function(
    image=image,
    secrets=[secrets],
    timeout=3600,
    retries=modal.Retries(max_retries=1, backoff_coefficient=2.0),
    schedule=modal.Cron("0 2 * * 1"),
)
def weekly_validation_cron():
    """Run weekly validation and literature cross-check batch."""
    import logging

    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    log = structlog.get_logger("modal.weekly_validation")
    log.info("weekly_validation_cron.start")

    try:
        from wced.pipeline.validation_weekly import run_weekly_validation

        run_weekly_validation()
        log.info("weekly_validation_cron.complete")
    except ImportError:
        log.warning(
            "weekly_validation_cron.skipped",
            reason="wced.pipeline.validation_weekly not yet implemented",
        )
