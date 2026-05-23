"""WCED operations CLI.

Entrypoint registered as ``wced`` via pyproject ``[project.scripts]``.
Each subcommand is a thin wrapper around library functions; the CLI itself
contains no business logic — that lives in ``wced.quantify``, ``wced.detect``,
etc.

Subcommands:

Inspection (read-only):
- ``wced factors list / show <key>``
- ``wced parameters list / show <key>``
- ``wced verify pending / show <id>``
- ``wced provenance show <id>``

State-changing (require ``--yes`` to skip confirmation):
- ``wced verify approve / reject / retract <id>``
- ``wced ingest firms / acled --date YYYY-MM-DD``
- ``wced detect --since YYYY-MM-DD``
- ``wced quantify --event <id> | --all-published``
- ``wced validate --event <id>``
- ``wced recompute --methodology-version X.Y``
- ``wced facility add`` (interactive)
- ``wced export --format csv --since YYYY-MM-DD``
- ``wced db migrate``

Every command emits a structured audit log record via ``structlog``. State-
changing commands prompt for confirmation unless ``--yes`` is supplied.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import structlog
import typer

from wced.cli.verify import app as verify_app
from wced.quantify.factors import (
    EmissionFactor,
    FactorRegistry,
    load_factors,
    load_parameter_distributions,
)

# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("WCED_LOG_LEVEL", "INFO"),
    format="%(message)s",
)
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
_audit_log = structlog.get_logger("wced.cli.audit")


def _audit(command: str, **kwargs: object) -> None:
    """Emit a structured audit-log entry for a CLI invocation.

    Why: CLAUDE.md mandates structured logging on every state-changing op
    so editorial actions can be reconstructed after the fact.
    """
    _audit_log.info(
        "cli.invoke",
        command=command,
        actor=os.environ.get("USER", "unknown"),
        at=datetime.now(UTC).isoformat(),
        **kwargs,
    )


def _confirm_or_abort(action: str, yes: bool) -> None:
    """Prompt for confirmation on destructive/state-changing actions."""
    if yes:
        return
    if not typer.confirm(f"About to {action}. Proceed?", default=False):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)


def _stub(operation: str, **details: object) -> None:
    """Render a 'not yet wired' notice in the same style as recompute.

    Used by commands whose orchestration targets (DB, Prefect flows) are not
    yet bootstrapped. Keeps the CLI surface stable while implementations land.
    """
    typer.echo(
        typer.style("⚠ Not yet wired to a persistent event store.", fg="yellow")
        + f" {operation} would run with:"
    )
    for k, v in details.items():
        typer.echo(f"  - {k}: {v}")

app = typer.Typer(
    help="WCED operations CLI.",
    no_args_is_help=True,
    add_completion=False,
)

factors_app = typer.Typer(
    help="Inspect emission factors loaded from data/emission_factors.yaml.",
    no_args_is_help=True,
)
parameters_app = typer.Typer(
    help="Inspect Monte Carlo priors loaded from data/parameter_distributions.yaml.",
    no_args_is_help=True,
)
app.add_typer(factors_app, name="factors")
app.add_typer(parameters_app, name="parameters")
app.add_typer(verify_app, name="verify")


def _format_factor_line(f: EmissionFactor) -> str:
    """One-line summary used by both ``list`` subcommands."""
    if f.distribution == "normal":
        params = f"mean={f.value}, sigma={f.sigma}"
    elif f.distribution in ("triangular",):
        params = f"low={f.low}, mode={f.mode}, high={f.high}"
    elif f.distribution == "uniform":
        params = f"low={f.low}, high={f.high}"
    else:  # constant
        params = f"value={f.value}"
    return f"{f.key}  [{f.distribution}]  {params}  ({f.units})  §{f.methodology_section}"


def _format_factor_full(f: EmissionFactor) -> str:
    """Pretty-printed JSON used by both ``show`` subcommands."""
    return json.dumps(f.model_dump(mode="json"), indent=2, sort_keys=True)


def _list_registry(registry: FactorRegistry) -> None:
    typer.echo(f"# Source: {registry.source_path}")
    for key in registry.keys():
        typer.echo(_format_factor_line(registry[key]))


def _show_one(registry: FactorRegistry, key: str) -> None:
    if key not in registry:
        typer.echo(
            f"Unknown key {key!r}. Known: {', '.join(registry.keys())}",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(_format_factor_full(registry[key]))


# ---------------------------------------------------------------------------
# factors
# ---------------------------------------------------------------------------


@factors_app.command("list")
def factors_list(
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Override path to the emission factors YAML.",
    ),
) -> None:
    """Print every emission factor loaded from the YAML file."""
    _list_registry(load_factors(path))


@factors_app.command("show")
def factors_show(
    key: str = typer.Argument(..., help="Factor key, e.g. crude_oil_combustion."),
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Override path to the emission factors YAML.",
    ),
) -> None:
    """Pretty-print one emission factor as JSON."""
    _show_one(load_factors(path), key)


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


@parameters_app.command("list")
def parameters_list(
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Override path to the parameter distributions YAML.",
    ),
) -> None:
    """Print every parameter prior loaded from the YAML file."""
    _list_registry(load_parameter_distributions(path))


@parameters_app.command("show")
def parameters_show(
    key: str = typer.Argument(..., help="Parameter key, e.g. burn_duty_cycle."),
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Override path to the parameter distributions YAML.",
    ),
) -> None:
    """Pretty-print one parameter prior as JSON."""
    _show_one(load_parameter_distributions(path), key)


@app.command("recompute")
def recompute(
    methodology_version: Annotated[
        str,
        typer.Option(
            "--methodology-version",
            help="Target methodology version string (e.g. '1.0.1').",
        ),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt."),
    ] = False,
) -> None:
    """Re-run quantification for all historical events under a new methodology version.

    Loads all PUBLISHED fire_events, recomputes baselines from FIRMS
    detection history, runs FRP (and inventory where damage assessments
    exist), reconciles, and writes new emission_estimate rows. Old
    estimates are preserved as historical record.

    All recomputed events are routed to PENDING_REVIEW — nothing is
    auto-published. Each transition is appended to the publication_log
    table. The run is tracked in recompute_runs (opened at start,
    closed at finish). A summary report is written to
    docs/RECOMPUTE_{version}_REPORT.md.
    """
    _confirm_or_abort(
        f"recompute ALL published estimates under methodology v{methodology_version}",
        yes,
    )
    _audit("recompute", methodology_version=methodology_version)

    from uuid import uuid4

    from shapely import wkt as shapely_wkt
    from sqlalchemy import func, select

    from wced.db import models
    from wced.db.repositories import (
        EmissionEstimateRepository,
        PostgisFacilityRepository,
        PublicationLogRepository,
        RecomputeRunRepository,
    )
    from wced.db.repositories.facility import _row_to_facility
    from wced.db.session import get_engine, get_session_factory
    from wced.detect.baseline import compute_baseline
    from wced.models.event import (
        ConfidenceLabel,
        DetectionSource,
        EventStatus,
        FireEvent,
    )
    from wced.db.repositories import ProvenanceRepository
    from wced.models.provenance import (
        ConfidenceLabel as ProvConfidence,
        ProvenanceRecord as ProvRecord,
        Source as ProvSource,
        SourceType,
    )
    from wced.pipeline.recompute import (
        EventRecomputeResult,
        PendingReviewTransition,
        RecomputeReport,
        generate_recompute_report_md,
        recompute_confidence_label,
        route_events_to_pending_review,
    )
    from wced.provenance.store import InMemoryProvenanceStore
    from wced.quantify.factors import load_factors, load_parameter_distributions
    from wced.quantify.frp import compute_frp_emissions
    from wced.quantify.inventory import compute_inventory_emissions
    from wced.quantify.reconcile import reconcile_estimates
    from wced.settings import get_settings

    typer.echo(
        f"Recomputing all estimates under methodology v{methodology_version}..."
    )

    settings = get_settings()
    factors = load_factors()
    params = load_parameter_distributions()
    engine = get_engine()
    Session = get_session_factory(engine)
    run_id = uuid4()
    run_started_at = datetime.now(tz=UTC)

    with Session() as session:
        # 0. Open recompute_runs row.
        recompute_repo = RecomputeRunRepository(session)
        recompute_repo.open_run(
            id=run_id,
            methodology_version=methodology_version,
            date_range_start=None,
            date_range_end=None,
            initiator=os.environ.get("USER", "unknown"),
            trigger="cli:recompute",
            started_at=run_started_at,
        )
        session.flush()
        typer.echo(f"Opened recompute run {run_id}.")

        # 1. Load all PUBLISHED fire_events.
        events_rows = session.execute(
            select(models.fire_events)
            .where(models.fire_events.c.status == "PUBLISHED")
            .order_by(models.fire_events.c.detected_at)
        ).all()
        typer.echo(f"Loaded {len(events_rows)} published fire events.")

        if not events_rows:
            typer.echo("No published events to recompute.")
            recompute_repo.close_run(
                run_id,
                status="COMPLETED",
                finished_at=datetime.now(tz=UTC),
                events_affected=0,
            )
            session.commit()
            return

        # 2. Load all facilities (with WKT geometry for spatial matching).
        facility_rows = session.execute(
            select(
                models.facilities.c.id,
                models.facilities.c.name,
                models.facilities.c.facility_type,
                func.ST_AsText(models.facilities.c.geometry).label("geometry_wkt"),
                models.facilities.c.country,
                models.facilities.c.capacity_barrels,
                models.facilities.c.capacity_uncertainty_pct,
                models.facilities.c.operator,
                models.facilities.c.source_url,
                models.facilities.c.added_at,
                models.facilities.c.notes,
            )
        ).all()
        facilities_by_id = {r.id: _row_to_facility(r) for r in facility_rows}
        typer.echo(f"Loaded {len(facilities_by_id)} facilities.")

        # 3. Build active-event windows per facility (for baseline exclusion).
        active_event_windows: dict[UUID, list[tuple[datetime, datetime]]] = {}
        for row in events_rows:
            r = row._asdict()
            fid = r["facility_id"]
            active_event_windows.setdefault(fid, []).append(
                (r["detected_at"], r["last_seen_at"])
            )

        # 4. Build per-facility FRP history from FIRMS detections.
        #    For each facility, query nearby detections using a lat/lon
        #    bounding box (~5 km buffer).
        facility_frp_history: dict[UUID, list[tuple[datetime, float]]] = {}
        _LAT_DELTA = 0.045  # ~5 km
        _LON_DELTA = 0.055  # ~5 km at ~35°N (Iran)

        for fid, facility in facilities_by_id.items():
            geom = facility.geometry()
            centroid = geom.centroid
            lat, lon = centroid.y, centroid.x

            nearby_stmt = (
                select(
                    models.firms_detections.c.acq_datetime,
                    models.firms_detections.c.frp,
                )
                .where(
                    models.firms_detections.c.frp.isnot(None),
                    models.firms_detections.c.latitude.between(
                        lat - _LAT_DELTA, lat + _LAT_DELTA
                    ),
                    models.firms_detections.c.longitude.between(
                        lon - _LON_DELTA, lon + _LON_DELTA
                    ),
                )
                .order_by(models.firms_detections.c.acq_datetime)
            )
            det_rows = session.execute(nearby_stmt).all()
            if det_rows:
                facility_frp_history[fid] = [
                    (r.acq_datetime, float(r.frp)) for r in det_rows
                ]

        total_detections = sum(len(v) for v in facility_frp_history.values())
        typer.echo(
            f"Loaded {total_detections} FIRMS detections across "
            f"{len(facility_frp_history)} facilities for baseline computation."
        )

        # 5. Recompute each event.
        prov_store = InMemoryProvenanceStore()
        prov_repo = ProvenanceRepository(session)
        baseline_cache: dict[UUID, object] = {}
        estimate_repo = EmissionEstimateRepository(session)
        n_written = 0
        n_skipped = 0
        n_zeroed = 0
        n_insufficient = 0
        n_inventory = 0
        firms_source_cache: dict[UUID, UUID] = {}

        for row in events_rows:
            r = row._asdict()
            if not r.get("total_frp_integral_mj") or r["total_frp_integral_mj"] <= 0:
                n_skipped += 1
                continue

            fe = FireEvent(
                id=r["id"],
                facility_id=r["facility_id"],
                detected_at=r["detected_at"],
                last_seen_at=r["last_seen_at"],
                peak_frp_mw=r["peak_frp_mw"],
                total_frp_integral_mj=r["total_frp_integral_mj"],
                detection_source=DetectionSource(r["detection_source"]),
                confidence_label=ConfidenceLabel(r["confidence_label"]),
                status=EventStatus(r["status"]),
                provenance_id=r["provenance_id"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                notes=r.get("notes"),
            )

            fid = fe.facility_id
            facility = facilities_by_id.get(fid)
            if facility is None:
                typer.echo(f"  ⚠ Event {fe.id}: facility {fid} not found, skipping.")
                n_skipped += 1
                continue

            # Compute or retrieve cached baseline.
            if fid not in baseline_cache:
                baseline_cache[fid] = compute_baseline(
                    fid,
                    facility_frp_history.get(fid, []),
                    active_event_windows=active_event_windows.get(fid),
                    store=prov_store,
                    reference_time=fe.detected_at,
                )
            baseline = baseline_cache[fid]

            # FRP method with baseline subtraction.
            frp_dist = compute_frp_emissions(
                fe, factors, n_samples=10_000, rng_seed=42, baseline=baseline,
                methodology_version=methodology_version,
            )

            if frp_dist.p50 == 0.0:
                n_zeroed += 1
            if baseline.is_fallback:
                n_insufficient += 1

            # Inventory method (if damage assessment exists).
            inventory_dist = None
            da_row = session.execute(
                select(models.damage_assessments)
                .where(models.damage_assessments.c.event_id == fe.id)
                .order_by(models.damage_assessments.c.assessed_at.desc())
                .limit(1)
            ).first()

            _INVENTORY_ELIGIBLE_TYPES = {"OIL_DEPOT", "STORAGE_TANK_FARM", "REFINERY"}
            if (
                da_row is not None
                and facility.capacity_barrels is not None
                and facility.facility_type.value in _INVENTORY_ELIGIBLE_TYPES
            ):
                da = da_row._asdict()
                try:
                    inventory_dist = compute_inventory_emissions(
                        event=fe,
                        facility=facility,
                        fraction_destroyed_pdf=(
                            da["fraction_destroyed_low"],
                            da["fraction_destroyed_mode"],
                            da["fraction_destroyed_high"],
                        ),
                        factors=factors,
                        params=params,
                        n_samples=10_000,
                        rng_seed=42,
                        methodology_version=methodology_version,
                    )
                    n_inventory += 1
                except (ValueError, KeyError) as exc:
                    typer.echo(f"  ⚠ Event {fe.id}: inventory method failed: {exc}")

            # Reconcile FRP + inventory.
            reconciliation = reconcile_estimates(
                event=fe,
                frp_estimate=frp_dist,
                inventory_estimate=inventory_dist,
                reported_estimate=None,
            )
            final_dist = reconciliation.final_distribution
            if final_dist is None:
                final_dist = frp_dist

            method = "FRP"
            if inventory_dist is not None and reconciliation.reconciled_ok:
                method = "RECONCILED"
            elif inventory_dist is not None:
                method = "FRP"

            now = datetime.now(tz=UTC)
            estimate_repo.insert(
                id=uuid4(),
                event_id=fe.id,
                methodology_version=methodology_version,
                method=method,
                p5=final_dist.p5,
                p50=final_dist.p50,
                p95=final_dist.p95,
                samples_ref=None,
                units="tCO2e",
                provenance_id=final_dist.provenance_id,
                parameter_versions={
                    "frp_to_combustion_rate": "1.0",
                    "carbon_recovery_as_co2": "1.0",
                    "burn_duty_cycle": "1.0",
                    "frp_extrapolation_factor": "1.0",
                    "baseline_method": "p75_iqr_v1.0.1",
                    "reconciliation": reconciliation.methodology_section,
                },
                created_at=now,
            )
            n_written += 1

            # --- Persist provenance chain to DB ---
            # 1. FIRMS source (one per facility, cached).
            if fid not in firms_source_cache:
                firms_src_id = uuid4()
                prov_repo.insert_source(
                    id=firms_src_id,
                    source_type="SATELLITE",
                    identifier=f"NASA FIRMS detections for facility {facility.name}",
                    retrieved_at=now,
                    content_hash=str(fid),
                    metadata={"facility_id": str(fid), "facility_name": facility.name},
                )
                firms_source_cache[fid] = firms_src_id
            firms_src_id = firms_source_cache[fid]

            # 1b. Detection-level provenance record (fire_event's own provenance_id).
            event_prov_id = fe.provenance_id
            if prov_repo.get_record(event_prov_id) is None:
                prov_repo.insert_record(
                    id=event_prov_id,
                    produced_by="wced.detect.hotspot",
                    method="firms_clustering_v1",
                    parameters={"detection_source": fe.detection_source.value},
                    produced_at=fe.detected_at,
                    confidence_label=fe.confidence_label.value,
                    notes=None,
                )
                prov_repo.link_input(event_prov_id, firms_src_id, "source")

            # 2. Baseline provenance record (from in-memory store).
            baseline_prov_id = baseline.provenance_id
            if prov_repo.get_record(baseline_prov_id) is None:
                baseline_node = prov_store.get(baseline_prov_id)
                prov_repo.insert_record(
                    id=baseline_node.id,
                    produced_by=baseline_node.produced_by,
                    method=baseline_node.method,
                    parameters=baseline_node.parameters,
                    produced_at=baseline_node.produced_at,
                    confidence_label=baseline_node.confidence_label.value,
                    notes=baseline_node.notes,
                )
                prov_repo.link_input(baseline_node.id, firms_src_id, "source")

            # 3. FRP computation record.
            frp_prov_id = frp_dist.provenance_id
            existing_frp = prov_repo.get_record(frp_prov_id)
            if existing_frp is None:
                prov_repo.insert_record(
                    id=frp_prov_id,
                    produced_by="wced.quantify.frp",
                    method=f"frp_to_co2_v{methodology_version}",
                    parameters={
                        "n_samples": 10_000,
                        "rng_seed": 42,
                        "net_frp_mj": float(frp_dist.p50),
                        "baseline_frp_mw": baseline.baseline_frp_mw,
                    },
                    produced_at=now,
                    confidence_label=fe.confidence_label.value,
                    notes=None,
                )
                prov_repo.link_input(frp_prov_id, event_prov_id, "provenance_record")
                prov_repo.link_input(frp_prov_id, baseline_prov_id, "provenance_record")
                prov_repo.link_input(frp_prov_id, firms_src_id, "source")

            # 4. If reconciled, create reconciliation record.
            if final_dist.provenance_id != frp_prov_id:
                recon_prov_id = final_dist.provenance_id
                existing_recon = prov_repo.get_record(recon_prov_id)
                if existing_recon is None:
                    prov_repo.insert_record(
                        id=recon_prov_id,
                        produced_by="wced.quantify.reconcile",
                        method=reconciliation.methodology_section,
                        parameters={"reconciled_ok": reconciliation.reconciled_ok},
                        produced_at=now,
                        confidence_label=fe.confidence_label.value,
                        notes=None,
                    )
                    prov_repo.link_input(recon_prov_id, frp_prov_id, "provenance_record")
                    if inventory_dist is not None:
                        inv_prov_id = inventory_dist.provenance_id
                        existing_inv = prov_repo.get_record(inv_prov_id)
                        if existing_inv is None:
                            prov_repo.insert_record(
                                id=inv_prov_id,
                                produced_by="wced.quantify.inventory",
                                method=f"inventory_v{methodology_version}",
                                parameters={"n_samples": 10_000, "rng_seed": 42},
                                produced_at=now,
                                confidence_label=fe.confidence_label.value,
                                notes=None,
                            )
                            prov_repo.link_input(inv_prov_id, firms_src_id, "source")
                        prov_repo.link_input(recon_prov_id, inv_prov_id, "provenance_record")

        # 6. Route all recomputed events to PENDING_REVIEW.
        pub_log_repo = PublicationLogRepository(session)
        fire_event_repo = __import__(
            "wced.db.repositories.fire_event", fromlist=["FireEventRepository"]
        ).FireEventRepository(session)
        recompute_results: list[EventRecomputeResult] = []
        n_routed = 0

        for row in events_rows:
            r = row._asdict()
            event_id = r["id"]
            old_label = ConfidenceLabel(r["confidence_label"])
            old_status = EventStatus(r["status"])
            facility = facilities_by_id.get(r["facility_id"])
            facility_name = facility.name if facility else "unknown"

            # Look up the old p50 from existing estimates (pre-recompute).
            old_estimate_row = session.execute(
                select(models.emission_estimates.c.p50)
                .where(models.emission_estimates.c.event_id == event_id)
                .where(
                    models.emission_estimates.c.methodology_version
                    != methodology_version
                )
                .order_by(models.emission_estimates.c.created_at.desc())
                .limit(1)
            ).first()
            old_p50 = float(old_estimate_row[0]) if old_estimate_row else 0.0

            # Look up the new p50 from estimates just written.
            new_estimate_row = session.execute(
                select(models.emission_estimates.c.p50)
                .where(models.emission_estimates.c.event_id == event_id)
                .where(
                    models.emission_estimates.c.methodology_version
                    == methodology_version
                )
                .order_by(models.emission_estimates.c.created_at.desc())
                .limit(1)
            ).first()
            new_p50 = float(new_estimate_row[0]) if new_estimate_row else 0.0

            # Look up corroboration metadata from provenance records.
            prov_rows = session.execute(
                select(models.provenance_records.c.parameters)
                .where(
                    models.provenance_records.c.method.like(
                        "confidence_assignment%"
                    )
                )
                .where(
                    models.provenance_records.c.id.in_(
                        select(models.provenance_inputs.c.input_id)
                        .where(
                            models.provenance_inputs.c.provenance_id
                            == r["provenance_id"]
                        )
                    )
                )
            ).all()

            has_acled = False
            has_gdelt = False
            has_s2_fire = False
            for prov_row in prov_rows:
                params = prov_row[0] or {}
                has_acled = has_acled or params.get("has_acled", False)
                has_gdelt = has_gdelt or params.get("has_gdelt", False)
                has_s2_fire = has_s2_fire or params.get(
                    "s2_confirms_fire", False
                )

            new_label = recompute_confidence_label(
                n_overpasses=2,
                s2_confirms_fire=has_s2_fire,
                has_acled_corroboration=has_acled,
                has_gdelt_corroboration=has_gdelt,
                enable_acled=settings.enable_acled,
            )

            label_changed = new_label != old_label

            # Update confidence_label on the fire_event row.
            if label_changed:
                session.execute(
                    models.fire_events.update()
                    .where(models.fire_events.c.id == event_id)
                    .values(confidence_label=new_label.value)
                )

            # Route to PENDING_REVIEW.
            route_now = datetime.now(tz=UTC)
            if old_status is not EventStatus.RETRACTED:
                fire_event_repo.update_status(
                    event_id, EventStatus.PENDING_REVIEW.value, route_now
                )
                pub_log_repo.append(
                    id=uuid4(),
                    target_type="fire_event",
                    target_id=event_id,
                    from_state=old_status.value,
                    to_state=EventStatus.PENDING_REVIEW.value,
                    action="recompute_route_to_review",
                    actor="wced:recompute",
                    reason=(
                        f"Recomputed under methodology v{methodology_version}"
                    ),
                    methodology_version=methodology_version,
                    created_at=route_now,
                )
                n_routed += 1

            recompute_results.append(EventRecomputeResult(
                event_id=event_id,
                facility_name=facility_name,
                old_label=old_label,
                new_label=new_label,
                old_p50_tco2e=old_p50,
                new_p50_tco2e=new_p50,
                had_acled_corroboration=has_acled,
                had_gdelt_corroboration=has_gdelt,
                had_s2_fire=has_s2_fire,
                label_changed=label_changed,
                routed_to_pending=old_status is not EventStatus.RETRACTED,
            ))

        # 7. Close recompute_runs row.
        run_finished_at = datetime.now(tz=UTC)
        recompute_repo.close_run(
            run_id,
            status="COMPLETED",
            finished_at=run_finished_at,
            events_affected=n_written,
        )

        session.commit()

    # 8. Generate report.
    report = RecomputeReport(
        methodology_version=methodology_version,
        run_id=run_id,
        started_at=run_started_at,
        finished_at=run_finished_at,
        events=recompute_results,
    )
    report_md = generate_recompute_report_md(report)
    report_path = (
        Path(__file__).parent.parent.parent
        / "docs"
        / f"RECOMPUTE_{methodology_version}_REPORT.md"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_md, encoding="utf-8")

    typer.echo(
        typer.style(
            f"✓ Wrote {n_written} emission estimates under methodology "
            f"v{methodology_version}.",
            fg="green",
        )
    )
    typer.echo(f"  {n_skipped} events skipped (missing FRP integral or facility)")
    typer.echo(f"  {n_zeroed} events zeroed out (net_frp=0 after baseline subtraction)")
    typer.echo(f"  {n_insufficient} events with insufficient_baseline_history flag")
    typer.echo(f"  {n_inventory} events with inventory method applied")
    typer.echo(f"  {len(firms_source_cache)} FIRMS source records persisted")
    typer.echo(f"  {n_routed} events routed to PENDING_REVIEW")
    typer.echo(f"  {report.labels_changed} confidence labels changed")
    typer.echo(f"  Report written to {report_path}")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

ingest_app = typer.Typer(
    help="Run data-source ingestion connectors for a given UTC day.",
    no_args_is_help=True,
)
app.add_typer(ingest_app, name="ingest")


def _parse_iso_date(value: str, field: str = "--date") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{field} must be YYYY-MM-DD; got {value!r}") from exc


@ingest_app.command("firms")
def ingest_firms(
    date_str: Annotated[
        str,
        typer.Option("--date", help="UTC day to ingest, YYYY-MM-DD."),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Fetch NASA FIRMS hotspots for one UTC day and persist them to firms_detections."""
    import asyncio
    from datetime import time as _time
    from uuid import uuid4

    from wced.db.repositories import FirmsDetectionRepository
    from wced.db.session import get_engine, get_session_factory
    from wced.ingest.firms import FIRMSConnector

    day = _parse_iso_date(date_str)
    _confirm_or_abort(f"ingest FIRMS hotspots for {day.isoformat()}", yes)
    _audit("ingest.firms", date=day.isoformat())

    map_key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if not map_key:
        typer.echo(
            typer.style("✗ FIRMS_MAP_KEY is not set in the environment.", fg="red"),
            err=True,
        )
        raise typer.Exit(code=1)

    # Iran + Gulf + Israel theatre, WGS84 (west, south, east, north).
    # Override with WCED_FIRMS_BBOX="w,s,e,n" if a different AOI is needed.
    bbox_env = os.environ.get("WCED_FIRMS_BBOX", "34.0,12.0,63.5,40.0")
    try:
        west, south, east, north = (float(x) for x in bbox_env.split(","))
    except ValueError as exc:
        raise typer.BadParameter(
            f"WCED_FIRMS_BBOX must be 'w,s,e,n'; got {bbox_env!r}"
        ) from exc

    start_dt = datetime.combine(day, _time(0, 0, tzinfo=UTC))
    end_dt = datetime.combine(day, _time(23, 59, 59, tzinfo=UTC))

    typer.echo(
        f"Ingesting FIRMS for {day.isoformat()} over bbox=({west},{south},{east},{north})..."
    )

    async def _stream() -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        async with FIRMSConnector(map_key) as conn:
            async for rec in conn.ingest(start_dt, end_dt, (west, south, east, north)):
                rows.append(rec)
        return rows

    raw_records = asyncio.run(_stream())
    typer.echo(f"Fetched {len(raw_records)} raw FIRMS rows.")

    now = datetime.now(tz=UTC)
    db_rows: list[dict[str, object]] = []
    for rec in raw_records:
        detected_at = rec.get("detected_at")
        if not isinstance(detected_at, datetime):
            continue
        bright_t31 = rec.get("bright_t31") or rec.get("bright_ti5")
        try:
            bright_t31_f = float(bright_t31) if bright_t31 not in (None, "") else None
        except (TypeError, ValueError):
            bright_t31_f = None
        # Drop the Source object before JSON-serialising the raw payload.
        raw_payload = {
            k: v for k, v in rec.items()
            if k != "_source" and not isinstance(v, datetime)
        }
        db_rows.append({
            "id": uuid4(),
            "latitude": float(rec["latitude"]),
            "longitude": float(rec["longitude"]),
            "brightness": rec.get("brightness") if isinstance(rec.get("brightness"), float) else None,
            "frp": rec.get("frp") if isinstance(rec.get("frp"), float) else None,
            "confidence": str(rec.get("confidence", "") or "") or None,
            "acq_datetime": detected_at,
            "satellite": str(rec.get("satellite", "") or ""),
            "instrument": str(rec.get("instrument", "") or ""),
            "version": str(rec.get("version", "") or "") or None,
            "bright_t31": bright_t31_f,
            "scan": rec.get("scan") if isinstance(rec.get("scan"), float) else None,
            "track": rec.get("track") if isinstance(rec.get("track"), float) else None,
            "raw_json": raw_payload,
            "ingested_at": now,
        })

    if not db_rows:
        typer.echo("No detections to persist.")
        return

    engine = get_engine()
    Session = get_session_factory(engine)
    with Session() as session:
        repo = FirmsDetectionRepository(session)
        inserted = repo.insert_batch(db_rows)
        session.commit()
    typer.echo(typer.style(f"✓ Inserted {inserted} FIRMS detections.", fg="green"))


@ingest_app.command("firms-historical")
def ingest_firms_historical(
    start_str: Annotated[
        str,
        typer.Option("--start", help="Start date (inclusive), YYYY-MM-DD."),
    ],
    end_str: Annotated[
        str,
        typer.Option("--end", help="End date (inclusive), YYYY-MM-DD."),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Backfill FIRMS archival data for a date range (uses standard-processing sources)."""
    import asyncio
    from datetime import time as _time
    from uuid import uuid4

    from wced.db.repositories import FirmsDetectionRepository
    from wced.db.session import get_engine, get_session_factory
    from wced.ingest.firms import FIRMSConnector

    start_day = _parse_iso_date(start_str, field="--start")
    end_day = _parse_iso_date(end_str, field="--end")
    if end_day < start_day:
        raise typer.BadParameter("--end must be >= --start")
    n_days = (end_day - start_day).days + 1
    _confirm_or_abort(
        f"ingest FIRMS archival data for {start_day} to {end_day} ({n_days} days)", yes
    )
    _audit("ingest.firms_historical", start=start_day.isoformat(), end=end_day.isoformat())

    map_key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if not map_key:
        typer.echo(
            typer.style("✗ FIRMS_MAP_KEY is not set in the environment.", fg="red"),
            err=True,
        )
        raise typer.Exit(code=1)

    bbox_env = os.environ.get("WCED_FIRMS_BBOX", "34.0,12.0,63.5,40.0")
    try:
        west, south, east, north = (float(x) for x in bbox_env.split(","))
    except ValueError as exc:
        raise typer.BadParameter(
            f"WCED_FIRMS_BBOX must be 'w,s,e,n'; got {bbox_env!r}"
        ) from exc

    start_dt = datetime.combine(start_day, _time(0, 0, tzinfo=UTC))
    end_dt = datetime.combine(end_day, _time(23, 59, 59, tzinfo=UTC))

    typer.echo(
        f"Ingesting FIRMS archive for {start_day} to {end_day} "
        f"({n_days} days) over bbox=({west},{south},{east},{north})..."
    )

    async def _stream() -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        async with FIRMSConnector(map_key, request_timeout=60.0) as conn:
            async for rec in conn.ingest_archive(start_dt, end_dt, (west, south, east, north)):
                rows.append(rec)
                if len(rows) % 10000 == 0:
                    print(f"  ... {len(rows)} records fetched so far", flush=True)
        return rows

    raw_records = asyncio.run(_stream())
    typer.echo(f"Fetched {len(raw_records)} raw FIRMS archival rows.")

    now = datetime.now(tz=UTC)
    db_rows: list[dict[str, object]] = []
    for rec in raw_records:
        detected_at = rec.get("detected_at")
        if not isinstance(detected_at, datetime):
            continue
        bright_t31 = rec.get("bright_t31") or rec.get("bright_ti5")
        try:
            bright_t31_f = float(bright_t31) if bright_t31 not in (None, "") else None
        except (TypeError, ValueError):
            bright_t31_f = None
        raw_payload = {
            k: v for k, v in rec.items()
            if k != "_source" and not isinstance(v, datetime)
        }
        db_rows.append({
            "id": uuid4(),
            "latitude": float(rec["latitude"]),
            "longitude": float(rec["longitude"]),
            "brightness": rec.get("brightness") if isinstance(rec.get("brightness"), float) else None,
            "frp": rec.get("frp") if isinstance(rec.get("frp"), float) else None,
            "confidence": str(rec.get("confidence", "") or "") or None,
            "acq_datetime": detected_at,
            "satellite": str(rec.get("satellite", "") or ""),
            "instrument": str(rec.get("instrument", "") or ""),
            "version": str(rec.get("version", "") or "") or None,
            "bright_t31": bright_t31_f,
            "scan": rec.get("scan") if isinstance(rec.get("scan"), float) else None,
            "track": rec.get("track") if isinstance(rec.get("track"), float) else None,
            "raw_json": raw_payload,
            "ingested_at": now,
        })

    if not db_rows:
        typer.echo("No archival detections to persist.")
        return

    engine = get_engine()
    Session = get_session_factory(engine)
    batch_size = 5000
    total_inserted = 0
    with Session() as session:
        repo = FirmsDetectionRepository(session)
        for i in range(0, len(db_rows), batch_size):
            batch = db_rows[i : i + batch_size]
            inserted = repo.insert_batch(batch)
            total_inserted += inserted
            typer.echo(f"  batch {i // batch_size + 1}: inserted {inserted} rows")
        session.commit()
    typer.echo(
        typer.style(f"✓ Inserted {total_inserted} archival FIRMS detections.", fg="green")
    )


@ingest_app.command("ucdp")
def ingest_ucdp(
    start_str: Annotated[
        str,
        typer.Option("--start", help="Start date (inclusive), YYYY-MM-DD."),
    ],
    end_str: Annotated[
        str,
        typer.Option("--end", help="End date (inclusive), YYYY-MM-DD."),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Backfill UCDP georeferenced conflict events for historical validation.

    UCDP data has months of latency — this is a validation/backfill source,
    not a real-time feed. Fetched events are logged for cross-validation
    against GDELT/FIRMS detections.
    """
    import asyncio

    start_day = _parse_iso_date(start_str, field="--start")
    end_day = _parse_iso_date(end_str, field="--end")
    if end_day < start_day:
        raise typer.BadParameter("--end must be >= --start")
    _confirm_or_abort(
        f"backfill UCDP events from {start_day} to {end_day}", yes,
    )
    _audit("ingest.ucdp", start=start_day.isoformat(), end=end_day.isoformat())

    from wced.ingest.ucdp import UCDPConnector

    typer.echo(
        f"Fetching UCDP GED events for {start_day} to {end_day} "
        f"(validation-only, not a real-time source)..."
    )

    async def _stream() -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        async with UCDPConnector() as conn:
            async for rec in conn.query_events(start_day, end_day):
                rows.append(rec)
        return rows

    raw_records = asyncio.run(_stream())
    typer.echo(f"Fetched {len(raw_records)} UCDP events.")

    for rec in raw_records:
        event = rec.get("event")
        if event is None:
            continue
        typer.echo(
            f"  {event.date_start} | {event.country:>12s} | "
            f"({event.latitude:.2f}, {event.longitude:.2f}) | "
            f"{event.conflict_name[:50]}"
        )

    typer.echo(
        typer.style(
            f"✓ {len(raw_records)} UCDP events available for validation.",
            fg="green",
        )
    )


@ingest_app.command("acled")
def ingest_acled(
    date_str: Annotated[
        str,
        typer.Option("--date", help="UTC day to ingest, YYYY-MM-DD."),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Fetch ACLED events for one UTC day and persist them to acled_events."""
    import asyncio
    from uuid import uuid4

    from wced.db.repositories import AcledEventRepository
    from wced.db.session import get_engine, get_session_factory
    from wced.ingest.acled import ACLEDConnector

    day = _parse_iso_date(date_str)
    _confirm_or_abort(f"ingest ACLED events for {day.isoformat()}", yes)
    _audit("ingest.acled", date=day.isoformat())

    email = os.environ.get("ACLED_EMAIL", "").strip()
    password = os.environ.get("ACLED_PASSWORD", "").strip()
    if not email or not password:
        typer.echo(
            typer.style(
                "✗ ACLED_EMAIL and ACLED_PASSWORD must be set in the environment.",
                fg="red",
            ),
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"Ingesting ACLED for {day.isoformat()}...")

    async def _stream() -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        async with ACLEDConnector(email, password) as conn:
            async for rec in conn.query_events(day, day):
                rows.append(rec)
        return rows

    raw_records = asyncio.run(_stream())
    typer.echo(f"Fetched {len(raw_records)} raw ACLED rows.")

    now = datetime.now(tz=UTC)
    db_rows: list[dict[str, object]] = []
    for rec in raw_records:
        event = rec.get("event")
        if event is None:
            continue
        # ACLED's stable numeric id is "data_id"; event_id_cnty is country-prefixed.
        try:
            acled_id = int(rec.get("data_id") or rec.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if not acled_id:
            continue
        raw_payload = {
            k: v for k, v in rec.items()
            if k not in {"_source", "event", "detected_at"}
        }
        db_rows.append({
            "id": uuid4(),
            "acled_id": acled_id,
            "event_date": event.event_date,
            "event_type": event.event_type,
            "sub_event_type": event.sub_event_type or None,
            "country": event.country,
            "admin1": str(rec.get("admin1", "") or "") or None,
            "admin2": str(rec.get("admin2", "") or "") or None,
            "location": event.location or None,
            "latitude": event.latitude,
            "longitude": event.longitude,
            "source": event.source or None,
            "notes": event.notes or None,
            "raw_json": raw_payload,
            "ingested_at": now,
        })

    if not db_rows:
        typer.echo("No ACLED events to persist.")
        return

    engine = get_engine()
    Session = get_session_factory(engine)
    with Session() as session:
        repo = AcledEventRepository(session)
        inserted = repo.upsert_batch(db_rows)
        session.commit()
    typer.echo(typer.style(f"✓ Upserted {inserted} ACLED events.", fg="green"))


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------


@app.command("detect")
def detect(
    since: Annotated[
        str,
        typer.Option("--since", help="Run detection for hotspots since this UTC date (YYYY-MM-DD)."),
    ],
    until: Annotated[
        str | None,
        typer.Option("--until", help="End date (exclusive) for the detection window (YYYY-MM-DD)."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
    auto_publish: Annotated[
        bool,
        typer.Option(
            "--auto-publish/--no-auto-publish",
            help="Dev convenience: mark detected events as PUBLISHED so they surface in the API.",
        ),
    ] = True,
) -> None:
    """Cluster firms_detections rows, match to facilities, and persist FireEvent rows."""
    from datetime import time as _time
    from uuid import uuid4

    from sqlalchemy import func, select

    from wced.db import models
    from wced.db.repositories import FireEventRepository
    from wced.db.session import get_engine, get_session_factory
    from wced.detect.facility_match import build_facility_tree, match_to_facility_with_tree
    from wced.detect.hotspot import FIRMSDetection, hotspots_to_candidates
    from wced.models.event import DetectionSource
    from wced.models.provenance import ConfidenceLabel
    from wced.provenance.store import InMemoryProvenanceStore

    since_day = _parse_iso_date(since, field="--since")
    since_dt = datetime.combine(since_day, _time(0, 0, tzinfo=UTC))
    until_dt = None
    if until is not None:
        until_day = _parse_iso_date(until, field="--until")
        until_dt = datetime.combine(until_day, _time(0, 0, tzinfo=UTC))
    label = f"since {since_day.isoformat()}" + (f" until {until}" if until else "")
    _confirm_or_abort(f"run detection for hotspots {label}", yes)
    _audit("detect", since=since_day.isoformat())
    typer.echo(f"Loading FIRMS detections {label}...")

    engine = get_engine()
    Session = get_session_factory(engine)

    with Session() as session:
        # 1. Pull all FIRMS detections in the window.
        stmt = (
            select(models.firms_detections)
            .where(models.firms_detections.c.acq_datetime >= since_dt)
            .order_by(models.firms_detections.c.acq_datetime)
        )
        if until_dt is not None:
            stmt = stmt.where(models.firms_detections.c.acq_datetime < until_dt)
        rows = session.execute(stmt).all()

        hotspots: list[FIRMSDetection] = []
        for r in rows:
            d = r._asdict()
            if d.get("frp") is None:
                continue
            instrument = (d.get("instrument") or "").upper()
            src = (
                DetectionSource.FIRMS_VIIRS
                if "VIIRS" in instrument
                else DetectionSource.FIRMS_MODIS
            )
            hotspots.append(
                FIRMSDetection(
                    id=d["id"],
                    latitude=float(d["latitude"]),
                    longitude=float(d["longitude"]),
                    frp_mw=float(d["frp"]),
                    detected_at=d["acq_datetime"],
                    detection_source=src,
                    brightness_k=float(d["brightness"]) if d.get("brightness") else 0.0,
                    confidence=d.get("confidence") or "",
                    source_id=d["id"],
                )
            )
        typer.echo(f"Loaded {len(hotspots)} hotspots with FRP.")

        if not hotspots:
            typer.echo("Nothing to cluster.")
            return

        # 2. Cluster hotspots into CandidateFireEvents.
        store = InMemoryProvenanceStore()
        candidates = hotspots_to_candidates(hotspots, store=store)
        typer.echo(f"Clustered into {len(candidates)} candidates.")

        # 3. Load all facilities, build STRtree.
        facility_rows = session.execute(
            select(
                models.facilities.c.id,
                models.facilities.c.name,
                models.facilities.c.facility_type,
                func.ST_AsText(models.facilities.c.geometry).label("geometry_wkt"),
                models.facilities.c.country,
                models.facilities.c.capacity_barrels,
                models.facilities.c.capacity_uncertainty_pct,
                models.facilities.c.operator,
                models.facilities.c.source_url,
                models.facilities.c.added_at,
                models.facilities.c.notes,
            )
        ).all()

        from wced.db.repositories.facility import _row_to_facility
        facilities = [_row_to_facility(r) for r in facility_rows]
        typer.echo(f"Loaded {len(facilities)} facilities.")
        if not facilities:
            typer.echo(
                typer.style(
                    "✗ No facilities registered. Run `just facility-load` (or `wced facility load`) first.",
                    fg="red",
                ),
                err=True,
            )
            raise typer.Exit(code=1)

        tree, facility_list = build_facility_tree(facilities)

        # 4. Match + persist matched candidates as fire_events.
        now = datetime.now(tz=UTC)
        fire_repo = FireEventRepository(session)
        n_matched = 0
        n_unmatched = 0
        for cand in candidates:
            facility, dist_m = match_to_facility_with_tree(
                cand, tree, facility_list, store=store
            )
            if facility is None:
                n_unmatched += 1
                continue

            # Trapezoidal FRP integral (MW·s = MJ) over peak-per-overpass.
            overpass_peak: dict[datetime, float] = {}
            for hs in cand.hotspots:
                t = hs.detected_at
                overpass_peak[t] = max(overpass_peak.get(t, 0.0), hs.frp_mw)
            times = sorted(overpass_peak.keys())
            peaks = [overpass_peak[t] for t in times]
            frp_integral_mj: float | None = None
            if len(times) >= 2:
                integral = 0.0
                for i in range(len(times) - 1):
                    dt_s = (times[i + 1] - times[i]).total_seconds()
                    integral += 0.5 * (peaks[i] + peaks[i + 1]) * dt_s
                frp_integral_mj = integral

            status = "PUBLISHED" if auto_publish else "PENDING_REVIEW"
            fire_repo.insert(
                id=uuid4(),
                facility_id=facility.id,
                detected_at=cand.first_detected_at,
                last_seen_at=cand.last_detected_at,
                peak_frp_mw=cand.peak_frp_mw,
                total_frp_integral_mj=frp_integral_mj,
                detection_source=cand.hotspots[0].detection_source.value,
                confidence_label=ConfidenceLabel.REPORTED.value,
                status=status,
                provenance_id=cand.provenance_id,
                created_at=now,
                updated_at=now,
                notes=f"dist_to_facility_m={dist_m:.0f}",
            )
            n_matched += 1
        session.commit()

    typer.echo(
        typer.style(
            f"✓ Detected {n_matched} fire events matched to facilities "
            f"({n_unmatched} unmatched candidates skipped).",
            fg="green",
        )
    )


# ---------------------------------------------------------------------------
# quantify
# ---------------------------------------------------------------------------


@app.command("quantify")
def quantify(
    event: Annotated[
        UUID | None,
        typer.Option("--event", help="Quantify a single event by UUID."),
    ] = None,
    all_published: Annotated[
        bool,
        typer.Option("--all-published", help="Quantify every PUBLISHED event."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Run the quantification stack (FRP + inventory + reconciliation) and write EmissionEstimate rows."""
    if event is None and not all_published:
        raise typer.BadParameter("Provide either --event <id> or --all-published.")
    if event is not None and all_published:
        raise typer.BadParameter("--event and --all-published are mutually exclusive.")

    target = f"event {event}" if event is not None else "ALL published events"
    _confirm_or_abort(f"quantify {target}", yes)
    _audit("quantify", event_id=str(event) if event else None, all_published=all_published)
    typer.echo(f"Quantifying {target}...")

    from uuid import uuid4

    from sqlalchemy import select

    from wced.db import models
    from wced.db.repositories import EmissionEstimateRepository
    from wced.db.session import get_engine, get_session_factory
    from wced.models.event import (
        ConfidenceLabel,
        DetectionSource,
        EventStatus,
        FireEvent,
    )
    from wced.detect.baseline import compute_baseline
    from wced.provenance.store import InMemoryProvenanceStore
    from wced.quantify.factors import load_factors
    from wced.quantify.frp import compute_frp_emissions

    factors = load_factors()
    engine = get_engine()
    Session = get_session_factory(engine)

    with Session() as session:
        q = select(models.fire_events)
        if event is not None:
            q = q.where(models.fire_events.c.id == event)
        else:
            q = q.where(models.fire_events.c.status == "PUBLISHED")
        events_rows = session.execute(q).all()
        typer.echo(f"Loaded {len(events_rows)} candidate event(s) for quantification.")

        # Build per-facility historical FRP from FIRMS detections for baseline.
        facility_frp_history: dict[UUID, list[tuple[datetime, float]]] = {}
        active_event_windows: dict[UUID, list[tuple[datetime, datetime]]] = {}
        for row in events_rows:
            r = row._asdict()
            fid = r["facility_id"]
            if r.get("total_frp_integral_mj") and r["total_frp_integral_mj"] > 0:
                active_event_windows.setdefault(fid, []).append(
                    (r["detected_at"], r["last_seen_at"])
                )

        # Query FIRMS detections matched to facilities for baseline computation.
        firms_stmt = (
            select(
                models.firms_detections.c.acq_datetime,
                models.firms_detections.c.frp,
                models.firms_detections.c.latitude,
                models.firms_detections.c.longitude,
            )
            .where(models.firms_detections.c.frp.isnot(None))
            .order_by(models.firms_detections.c.acq_datetime)
        )
        firms_rows = session.execute(firms_stmt).all()
        typer.echo(f"Loaded {len(firms_rows)} FIRMS detections for baseline computation.")

        prov_store = InMemoryProvenanceStore()
        baseline_cache: dict[UUID, object] = {}

        estimate_repo = EmissionEstimateRepository(session)
        n_written = 0
        n_skipped = 0
        for row in events_rows:
            r = row._asdict()
            if not r.get("total_frp_integral_mj") or r["total_frp_integral_mj"] <= 0:
                n_skipped += 1
                continue
            fe = FireEvent(
                id=r["id"],
                facility_id=r["facility_id"],
                detected_at=r["detected_at"],
                last_seen_at=r["last_seen_at"],
                peak_frp_mw=r["peak_frp_mw"],
                total_frp_integral_mj=r["total_frp_integral_mj"],
                detection_source=DetectionSource(r["detection_source"]),
                confidence_label=ConfidenceLabel(r["confidence_label"]),
                status=EventStatus(r["status"]),
                provenance_id=r["provenance_id"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                notes=r.get("notes"),
            )

            fid = fe.facility_id
            if fid not in baseline_cache:
                baseline_cache[fid] = compute_baseline(
                    fid,
                    facility_frp_history.get(fid, []),
                    active_event_windows=active_event_windows.get(fid),
                    store=prov_store,
                    reference_time=fe.detected_at,
                )
            baseline = baseline_cache[fid]

            dist = compute_frp_emissions(
                fe, factors, n_samples=10_000, rng_seed=42, baseline=baseline
            )
            estimate_repo.insert(
                id=uuid4(),
                event_id=fe.id,
                methodology_version=dist.methodology_version,
                method="FRP",
                p5=dist.p5,
                p50=dist.p50,
                p95=dist.p95,
                samples_ref=None,
                units="tCO2e",
                provenance_id=dist.provenance_id,
                parameter_versions={
                    "frp_to_combustion_rate": "1.0",
                    "carbon_recovery_as_co2": "1.0",
                    "burn_duty_cycle": "1.0",
                    "frp_extrapolation_factor": "1.0",
                    "baseline_method": "p75_iqr_v1.0.1",
                },
                created_at=datetime.now(tz=UTC),
            )
            n_written += 1
        session.commit()

    typer.echo(
        typer.style(
            f"✓ Wrote {n_written} emission estimates "
            f"({n_skipped} events skipped — missing FRP integral).",
            fg="green",
        )
    )


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@app.command("validate")
def validate(
    event: Annotated[
        UUID,
        typer.Option("--event", help="Event UUID to validate against TROPOMI NO2."),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Cross-validate one event's estimate against TROPOMI satellite plume data."""
    _confirm_or_abort(f"run TROPOMI validation for event {event}", yes)
    _audit("validate", event=str(event))
    typer.echo(f"Validating event {event} against TROPOMI...")
    _stub(
        "Validation",
        event=str(event),
        steps="validate.tropomi.plume_overlap() → write ValidationResult row",
    )


# ---------------------------------------------------------------------------
# facility
# ---------------------------------------------------------------------------

facility_app = typer.Typer(
    help="Manage the facility registry (data/facilities/*.geojson).",
    no_args_is_help=True,
)
app.add_typer(facility_app, name="facility")


@facility_app.command("load")
def facility_load(
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Override the default facilities GeoJSON."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Load (upsert) the seeded facility GeoJSON into the `facilities` table."""
    from wced.db.repositories import PostgisFacilityRepository
    from wced.db.session import get_engine, get_session_factory

    _confirm_or_abort(
        f"load facility registry from {path or '<default>'}", yes
    )
    _audit("facility.load", path=str(path) if path else None)
    engine = get_engine()
    Session = get_session_factory(engine)
    with Session() as session:
        repo = PostgisFacilityRepository(session)
        if path is not None:
            schema_path = path.parent / "facilities.schema.json"
            n = repo.load_geojson(
                path, schema_path=schema_path if schema_path.exists() else None
            )
        else:
            n = repo.load_geojson()
        session.commit()
    typer.echo(typer.style(f"✓ Loaded {n} facilities.", fg="green"))


@facility_app.command("add")
def facility_add(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Interactively register a new facility in the registry."""
    typer.echo("Adding a new facility to the registry. Empty input aborts.\n")
    name = typer.prompt("Facility name")
    country = typer.prompt("Country (ISO-3, e.g. IRN, ISR)")
    facility_type = typer.prompt(
        "Facility type (REFINERY|OIL_DEPOT|GAS_PROCESSING|OIL_FIELD|PIPELINE_STATION)"
    )
    lat = float(typer.prompt("Latitude (decimal degrees, WGS84)"))
    lon = float(typer.prompt("Longitude (decimal degrees, WGS84)"))
    capacity = typer.prompt("Nameplate capacity (free-form, e.g. '250000 bbl/day')", default="")
    notes = typer.prompt("Notes (optional)", default="")

    _confirm_or_abort(
        f"register facility {name!r} ({facility_type}) at ({lat}, {lon})", yes
    )
    _audit(
        "facility.add",
        name=name,
        country=country,
        facility_type=facility_type,
        lat=lat,
        lon=lon,
    )
    typer.echo(typer.style(f"✓ Drafted facility {name!r}.", fg="green"))
    _stub(
        "Facility add",
        record={
            "name": name,
            "country": country,
            "facility_type": facility_type,
            "lat": lat,
            "lon": lon,
            "capacity": capacity,
            "notes": notes,
        },
        steps="dual-use review checklist → write to data/facilities/<country>.geojson → git commit",
    )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@app.command("export")
def export(
    fmt: Annotated[
        str,
        typer.Option("--format", help="Output format: csv | json | geojson."),
    ] = "csv",
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only include events on/after this UTC date."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output file path (default: stdout)."),
    ] = None,
) -> None:
    """Export published incidents and estimates under CC-BY 4.0.

    Why: open-methodology / open-data commitment in CLAUDE.md.
    """
    if fmt not in {"csv", "json", "geojson"}:
        raise typer.BadParameter(f"--format must be csv|json|geojson; got {fmt!r}")
    since_day = _parse_iso_date(since, field="--since") if since else None

    _audit(
        "export",
        format=fmt,
        since=since_day.isoformat() if since_day else None,
        output=str(output) if output else "stdout",
    )
    typer.echo(
        f"Exporting published events (format={fmt}, since={since_day}, "
        f"license='CC-BY 4.0') → {output or 'stdout'}"
    )
    _stub(
        "Export",
        format=fmt,
        since=since_day.isoformat() if since_day else "<all>",
        steps="SELECT PUBLISHED events ⨝ estimates → render → attach CC-BY 4.0 header",
    )


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------

db_app = typer.Typer(help="Database operations.", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command("migrate")
def db_migrate(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Apply pending Alembic migrations to the configured database."""
    dsn = (
        os.environ.get("WCED_DB_DSN")
        or os.environ.get("WCED_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "<unset>"
    )
    _confirm_or_abort(f"apply Alembic migrations against {dsn}", yes)
    _audit("db.migrate", dsn_set=dsn != "<unset>")
    typer.echo(f"Running alembic upgrade head against {dsn}...")

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    if dsn != "<unset>":
        alembic_cfg.set_main_option("sqlalchemy.url", dsn)
    command.upgrade(alembic_cfg, "head")
    typer.echo(typer.style("✓ Migrations applied.", fg="green"))


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------

backfill_app = typer.Typer(
    help="Backfill historical data from validation-only sources.",
    no_args_is_help=True,
)
app.add_typer(backfill_app, name="backfill")


@backfill_app.command("ucdp")
def backfill_ucdp(
    from_str: Annotated[
        str,
        typer.Option("--from", help="Start date (inclusive), YYYY-MM-DD."),
    ],
    to_str: Annotated[
        str,
        typer.Option("--to", help="End date (inclusive), YYYY-MM-DD."),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Backfill UCDP georeferenced events for historical validation.

    Alias for ``wced ingest ucdp``. UCDP data has months of latency and is
    used exclusively for cross-validating GDELT/FIRMS detections, not for
    real-time monitoring.
    """
    ingest_ucdp(start_str=from_str, end_str=to_str, yes=yes)


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------

provenance_app = typer.Typer(
    help="Inspect provenance records and DAGs.",
    no_args_is_help=True,
)
app.add_typer(provenance_app, name="provenance")


@provenance_app.command("show")
def provenance_show(
    provenance_id: Annotated[UUID, typer.Argument(help="Provenance record UUID.")],
) -> None:
    """Render the provenance DAG rooted at <id> as indented text."""
    _audit("provenance.show", provenance_id=str(provenance_id))
    typer.echo(f"Provenance DAG for {provenance_id}:")
    _stub(
        "Provenance render",
        root=str(provenance_id),
        steps="provenance.store.get(id) → walk parents recursively → indent-render nodes by kind/source",
    )


if __name__ == "__main__":  # pragma: no cover
    app()
