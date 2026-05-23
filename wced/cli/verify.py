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
from typing import Annotated
from uuid import UUID

import typer

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
) -> None:
    """Approve an event and publish it to the dashboard."""
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
