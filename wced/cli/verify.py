"""CLI subcommands for the editorial review queue.

Usage:
  wced verify pending
  wced verify show <id>
  wced verify approve <id> --reviewer NAME [--notes TEXT]
  wced verify reject  <id> --reviewer NAME --reason TEXT
  wced verify retract <id> --reviewer NAME --reason TEXT

The queue backend is selected from the environment:
  WCED_DB_DSN set → PostgresReviewQueue (stub until the DB prompt)
  otherwise      → InMemoryReviewQueue (exits after the process ends)

For local development and CI, InMemory is sufficient. Production deployments
must set WCED_DB_DSN so state persists across runs.

CLAUDE.md: "For V1, every event goes through manual editorial review. We do
not auto-publish, even high-confidence events, in the first 6 months."
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

import typer

from wced.models.assessment import AssessmentMethod, DamageAssessment
from wced.models.editorial import EditorialAction, EditorialTransitionError
from wced.models.event import EventStatus, FireEvent
from wced.verify.editorial import InMemoryReviewQueue, PostgresReviewQueue

app = typer.Typer(
    help="Editorial review queue for fire events.",
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------
# Queue factory
# ---------------------------------------------------------------------------

_queue: InMemoryReviewQueue | PostgresReviewQueue | None = None
_assessments: dict[UUID, DamageAssessment] = {}


def _get_queue() -> InMemoryReviewQueue | PostgresReviewQueue:
    global _queue
    if _queue is None:
        dsn = os.environ.get("WCED_DB_DSN", "")
        if dsn:
            _queue = PostgresReviewQueue(dsn)
        else:
            _queue = InMemoryReviewQueue()
    return _queue


def _inject_queue(queue: InMemoryReviewQueue) -> None:
    """Override the queue for tests; not part of the public CLI surface."""
    global _queue
    _queue = queue


def get_assessments() -> dict[UUID, DamageAssessment]:
    """Return the in-memory assessment store (event_id → DamageAssessment)."""
    return _assessments


def _parse_fraction_destroyed(value: str) -> tuple[float, float, float]:
    """Parse a 'low,mode,high' string into a validated triple.

    Raises
    ------
    typer.BadParameter
        If the string is malformed or values are out of [0, 1].
    """
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 3:
        raise typer.BadParameter(
            f"Expected 'low,mode,high' (3 comma-separated floats); got {value!r}"
        )
    try:
        low, mode, high = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError as exc:
        raise typer.BadParameter(f"Non-numeric value in fraction-destroyed: {exc}") from exc
    if not (0.0 <= low <= mode <= high <= 1.0):
        raise typer.BadParameter(
            f"fraction-destroyed must satisfy 0 <= low <= mode <= high <= 1; "
            f"got ({low}, {mode}, {high})"
        )
    return low, mode, high


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

_STATUS_COLOUR: dict[EventStatus, str] = {
    EventStatus.PENDING_REVIEW: "yellow",
    EventStatus.PUBLISHED: "green",
    EventStatus.REJECTED: "red",
    EventStatus.RETRACTED: "bright_black",
}


def _status_badge(status: EventStatus) -> str:
    colour = _STATUS_COLOUR.get(status, "white")
    return typer.style(f"[{status.value}]", fg=colour, bold=True)


def _fmt_event(event: FireEvent) -> str:
    lines = [
        f"id          : {event.id}",
        f"facility_id : {event.facility_id}",
        f"status      : {_status_badge(event.status)}",
        f"confidence  : {event.confidence_label.value}",
        f"detected_at : {event.detected_at.isoformat()}",
        f"last_seen   : {event.last_seen_at.isoformat()}",
        f"peak_frp_mw : {event.peak_frp_mw:.1f} MW",
        f"source      : {event.detection_source.value}",
    ]
    if event.notes:
        lines.append(f"notes       : {event.notes}")
    return "\n".join(lines)


def _fmt_action(action: EditorialAction, idx: int) -> str:
    colour = "green" if action.action_type.value in ("APPROVED",) else (
        "red" if action.action_type.value in ("REJECTED", "RETRACTED") else "white"
    )
    header = typer.style(
        f"#{idx + 1}  {action.action_type.value}",
        fg=colour,
        bold=True,
    )
    lines = [
        header,
        f"    reviewer : {action.reviewer}",
        f"    at       : {action.acted_at.isoformat()}",
        f"    {action.previous_status.value} → {action.new_status.value}",
    ]
    if action.notes:
        lines.append(f"    notes    : {action.notes}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("pending")
def cmd_pending() -> None:
    """List all events currently awaiting editorial review."""
    queue = _get_queue()
    events = queue.pending()
    if not events:
        typer.echo("No events pending review.")
        return
    typer.echo(f"{len(events)} event(s) pending review:\n")
    for event in events:
        typer.echo(_fmt_event(event))
        typer.echo()


@app.command("show")
def cmd_show(
    event_id: Annotated[UUID, typer.Argument(help="UUID of the fire event.")],
) -> None:
    """Show full details and editorial history for one event."""
    queue = _get_queue()
    try:
        event = queue.get(event_id)
    except KeyError:
        typer.echo(f"Event {event_id} not found.", err=True)
        raise typer.Exit(code=1)

    typer.echo("=== Event ===")
    typer.echo(_fmt_event(event))
    typer.echo()

    actions = queue.history(event_id)
    if actions:
        typer.echo("=== Editorial History ===")
        for i, action in enumerate(actions):
            typer.echo(_fmt_action(action, i))
            typer.echo()
    else:
        typer.echo("No editorial history recorded.")


@app.command("approve")
def cmd_approve(
    event_id: Annotated[UUID, typer.Argument(help="UUID of the event to approve.")],
    reviewer: Annotated[str, typer.Option("--reviewer", "-r", help="Reviewer identity.")],
    notes: Annotated[
        str | None,
        typer.Option("--notes", "-n", help="Optional approval notes."),
    ] = None,
    fraction_destroyed: Annotated[
        str | None,
        typer.Option(
            "--fraction-destroyed", "-f",
            help="Damage assessment as 'low,mode,high' (e.g. '0.25,0.40,0.55'). "
                 "Values in [0,1]. Creates a DamageAssessment for inventory estimation.",
        ),
    ] = None,
    assessment_method: Annotated[
        AssessmentMethod,
        typer.Option(
            "--assessment-method",
            help="How the fraction-destroyed estimate was derived.",
        ),
    ] = AssessmentMethod.EXPERT_ESTIMATE,
) -> None:
    """Approve an event and publish it to the dashboard.

    Optionally attach a DamageAssessment with --fraction-destroyed to enable
    inventory-based emission estimation for this event.
    """
    queue = _get_queue()
    try:
        event = queue.approve(event_id, reviewer=reviewer, notes=notes)
    except KeyError:
        typer.echo(f"Event {event_id} not found.", err=True)
        raise typer.Exit(code=1)
    except EditorialTransitionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    typer.echo(
        typer.style("✓ Approved", fg="green", bold=True)
        + f" — event {event_id} is now PUBLISHED."
    )
    if notes:
        typer.echo(f"  notes: {notes}")

    if fraction_destroyed is not None:
        low, mode, high = _parse_fraction_destroyed(fraction_destroyed)
        assessment = DamageAssessment(
            event_id=event_id,
            facility_id=event.facility_id,
            fraction_destroyed_low=low,
            fraction_destroyed_mode=mode,
            fraction_destroyed_high=high,
            assessed_by=reviewer,
            assessment_method=assessment_method,
            notes=notes,
            assessed_at=datetime.now(UTC),
            provenance_id=event.provenance_id,
        )
        _assessments[event_id] = assessment
        typer.echo(
            f"  damage assessment: ψ ~ Triangular({low}, {mode}, {high}) "
            f"[{assessment_method.value}]"
        )


@app.command("add-assessment")
def cmd_add_assessment(
    event_id: Annotated[UUID, typer.Argument(help="UUID of the event.")],
    reviewer: Annotated[str, typer.Option("--reviewer", "-r", help="Reviewer identity.")],
    fraction_destroyed: Annotated[
        str,
        typer.Option(
            "--fraction-destroyed", "-f",
            help="Damage assessment as 'low,mode,high' (e.g. '0.25,0.40,0.55'). Values in [0,1].",
        ),
    ],
    assessment_method: Annotated[
        AssessmentMethod,
        typer.Option(
            "--assessment-method",
            help="How the fraction-destroyed estimate was derived.",
        ),
    ] = AssessmentMethod.EXPERT_ESTIMATE,
    notes: Annotated[
        str | None,
        typer.Option("--notes", "-n", help="Optional notes."),
    ] = None,
) -> None:
    """Attach a DamageAssessment to an already-PUBLISHED event.

    Unlike approve (which transitions PENDING_REVIEW -> PUBLISHED), this
    command works on events that are already PUBLISHED and only inserts a
    damage_assessments row so inventory-based estimation can proceed.
    """
    low, mode, high = _parse_fraction_destroyed(fraction_destroyed)

    dsn = os.environ.get("WCED_DB_DSN", "")
    if not dsn:
        typer.echo(
            typer.style("✗ WCED_DB_DSN must be set for add-assessment.", fg="red"),
            err=True,
        )
        raise typer.Exit(code=1)

    from sqlalchemy import select

    from wced.db import models
    from wced.db.repositories import DamageAssessmentRepository
    from wced.db.session import get_engine, get_session_factory

    engine = get_engine()
    Session = get_session_factory(engine)
    with Session() as session:
        row = session.execute(
            select(models.fire_events).where(models.fire_events.c.id == event_id)
        ).first()
        if row is None:
            typer.echo(f"Event {event_id} not found.", err=True)
            raise typer.Exit(code=1)
        ev = row._asdict()
        if ev["status"] != "PUBLISHED":
            typer.echo(
                f"Event {event_id} is {ev['status']}; expected PUBLISHED.",
                err=True,
            )
            raise typer.Exit(code=1)

        repo = DamageAssessmentRepository(session)
        da_id = uuid4()
        repo.insert(
            id=da_id,
            event_id=event_id,
            facility_id=ev["facility_id"],
            fraction_destroyed_low=low,
            fraction_destroyed_mode=mode,
            fraction_destroyed_high=high,
            assessed_by=reviewer,
            assessment_method=assessment_method.value,
            notes=notes,
            assessed_at=datetime.now(UTC),
            provenance_id=ev["provenance_id"],
        )
        session.commit()

    typer.echo(
        typer.style("✓ Assessment added", fg="green", bold=True)
        + f" — event {event_id}: ψ ~ Triangular({low}, {mode}, {high}) "
        f"[{assessment_method.value}]"
    )


@app.command("reject")
def cmd_reject(
    event_id: Annotated[UUID, typer.Argument(help="UUID of the event to reject.")],
    reviewer: Annotated[str, typer.Option("--reviewer", "-r", help="Reviewer identity.")],
    reason: Annotated[str, typer.Option("--reason", help="Mandatory rejection reason.")],
) -> None:
    """Reject an event with a documented reason."""
    queue = _get_queue()
    try:
        queue.reject(event_id, reviewer=reviewer, reason=reason)
    except KeyError:
        typer.echo(f"Event {event_id} not found.", err=True)
        raise typer.Exit(code=1)
    except (EditorialTransitionError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    typer.echo(
        typer.style("✗ Rejected", fg="red", bold=True)
        + f" — event {event_id}."
    )
    typer.echo(f"  reason: {reason}")


@app.command("retract")
def cmd_retract(
    event_id: Annotated[UUID, typer.Argument(help="UUID of the event to retract.")],
    reviewer: Annotated[str, typer.Option("--reviewer", "-r", help="Reviewer identity.")],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Mandatory retraction reason (public changelog entry)."),
    ],
) -> None:
    """Retract a published event. The reason is surfaced as a public changelog entry."""
    queue = _get_queue()
    try:
        queue.retract(event_id, reviewer=reviewer, reason=reason)
    except KeyError:
        typer.echo(f"Event {event_id} not found.", err=True)
        raise typer.Exit(code=1)
    except (EditorialTransitionError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    typer.echo(
        typer.style("⚠ Retracted", fg="bright_black", bold=True)
        + f" — event {event_id}. Changelog entry recorded."
    )
    typer.echo(f"  reason: {reason}")
