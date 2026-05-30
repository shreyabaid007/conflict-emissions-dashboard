"""Recompute orchestration logic for methodology version bumps.

Provides testable, DB-independent functions for:
  - Confidence label recomputation under a new methodology version
  - Generating before/after recompute reports
  - Routing recomputed events to PENDING_REVIEW with audit trail

The CLI ``wced recompute`` command delegates to these functions after
loading data from the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from wced.models.event import EventStatus, FireEvent
from wced.models.provenance import ConfidenceLabel


@dataclass(frozen=True)
class EventRecomputeResult:
    """Before/after snapshot for a single event's recompute."""

    event_id: UUID
    facility_name: str
    old_label: ConfidenceLabel
    new_label: ConfidenceLabel
    old_p50_tco2e: float
    new_p50_tco2e: float
    had_acled_corroboration: bool
    had_gdelt_corroboration: bool
    had_s2_fire: bool
    label_changed: bool
    routed_to_pending: bool


@dataclass
class RecomputeReport:
    """Aggregated recompute results for report generation."""

    methodology_version: str
    run_id: UUID
    started_at: datetime
    finished_at: datetime | None = None
    events: list[EventRecomputeResult] = field(default_factory=list)

    @property
    def total_events(self) -> int:
        return len(self.events)

    @property
    def labels_changed(self) -> int:
        return sum(1 for e in self.events if e.label_changed)

    @property
    def labels_unchanged(self) -> int:
        return sum(1 for e in self.events if not e.label_changed)

    @property
    def upgraded(self) -> list[EventRecomputeResult]:
        _order = list(ConfidenceLabel)
        return [
            e for e in self.events
            if e.label_changed
            and _order.index(e.new_label) < _order.index(e.old_label)
        ]

    @property
    def downgraded(self) -> list[EventRecomputeResult]:
        _order = list(ConfidenceLabel)
        return [
            e for e in self.events
            if e.label_changed
            and _order.index(e.new_label) > _order.index(e.old_label)
        ]

    @property
    def acled_only_events(self) -> list[EventRecomputeResult]:
        return [
            e for e in self.events
            if e.had_acled_corroboration and not e.had_gdelt_corroboration
        ]

    @property
    def old_total_p50(self) -> float:
        return sum(e.old_p50_tco2e for e in self.events)

    @property
    def new_total_p50(self) -> float:
        return sum(e.new_p50_tco2e for e in self.events)


def recompute_confidence_label(
    *,
    n_overpasses: int,
    s2_confirms_fire: bool,
    has_acled_corroboration: bool,
    has_gdelt_corroboration: bool,
    enable_acled: bool,
) -> ConfidenceLabel:
    """Apply the v1.1.0 source-agnostic confidence decision table.

    When ``enable_acled`` is False, ACLED corroboration is ignored even
    if historical matches exist in the database.

    This is a pure function suitable for testing without DB access.
    """
    persistent = n_overpasses >= 2

    has_acled = has_acled_corroboration and enable_acled
    has_any_corroboration = has_acled or has_gdelt_corroboration

    if persistent and s2_confirms_fire and has_any_corroboration:
        return ConfidenceLabel.CONFIRMED
    elif persistent and s2_confirms_fire:
        return ConfidenceLabel.VERIFIED
    elif persistent and has_any_corroboration:
        return ConfidenceLabel.VERIFIED
    elif persistent:
        return ConfidenceLabel.REPORTED
    else:
        return ConfidenceLabel.SUSPECTED


@dataclass
class PendingReviewTransition:
    """Record of an event routed to PENDING_REVIEW during recompute."""

    event_id: UUID
    previous_status: EventStatus
    new_status: EventStatus = EventStatus.PENDING_REVIEW
    actor: str = "wced:recompute"
    reason: str | None = None
    methodology_version: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def as_publication_log_entry(self) -> dict[str, Any]:
        return {
            "id": uuid4(),
            "target_type": "fire_event",
            "target_id": self.event_id,
            "from_state": self.previous_status.value,
            "to_state": self.new_status.value,
            "action": "recompute_route_to_review",
            "actor": self.actor,
            "reason": self.reason,
            "methodology_version": self.methodology_version,
            "created_at": self.created_at,
        }


def route_events_to_pending_review(
    events: list[FireEvent],
    *,
    methodology_version: str,
) -> list[PendingReviewTransition]:
    """Build PENDING_REVIEW transition records for all recomputed events.

    Every recomputed event is routed to PENDING_REVIEW regardless of its
    previous status. This ensures human approval before publication under
    the new methodology version.

    Returns transition records (not yet persisted) so the caller can
    write them to the publication_log in a single transaction.
    """
    transitions: list[PendingReviewTransition] = []
    for event in events:
        if event.status is EventStatus.RETRACTED:
            continue
        transitions.append(PendingReviewTransition(
            event_id=event.id,
            previous_status=event.status,
            reason=f"Recomputed under methodology v{methodology_version}",
            methodology_version=methodology_version,
        ))
    return transitions


def generate_recompute_report_md(report: RecomputeReport) -> str:
    """Render a RecomputeReport as markdown for docs/RECOMPUTE_*.md."""
    lines: list[str] = []
    lines.append(f"# Recompute Report — v{report.methodology_version}")
    lines.append("")
    lines.append(f"> Run ID: `{report.run_id}`")
    lines.append(f"> Started: {report.started_at.isoformat()}")
    if report.finished_at:
        lines.append(f"> Finished: {report.finished_at.isoformat()}")
    lines.append(f"> Events processed: {report.total_events}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Headline totals
    lines.append("## Headline totals")
    lines.append("")
    lines.append(f"| Metric | Before | After | Change |")
    lines.append(f"|--------|--------|-------|--------|")
    old_total = report.old_total_p50
    new_total = report.new_total_p50
    delta = new_total - old_total
    pct = (delta / old_total * 100) if old_total else 0
    lines.append(
        f"| Total p50 (tCO2e) | {old_total:,.1f} | {new_total:,.1f} "
        f"| {delta:+,.1f} ({pct:+.1f}%) |"
    )
    lines.append("")

    # Label change summary
    lines.append("## Confidence label changes")
    lines.append("")
    lines.append(f"- **Changed:** {report.labels_changed}")
    lines.append(f"- **Unchanged:** {report.labels_unchanged}")
    lines.append(f"- **Upgraded:** {len(report.upgraded)}")
    lines.append(f"- **Downgraded:** {len(report.downgraded)}")
    lines.append("")

    # ACLED-only events
    acled_only = report.acled_only_events
    if acled_only:
        lines.append("## Events with ACLED-only corroboration")
        lines.append("")
        lines.append(
            "These events had ACLED corroboration but no GDELT match. "
            "With `ENABLE_ACLED=False`, their ACLED corroboration is "
            "excluded from the confidence computation."
        )
        lines.append("")
        lines.append("| Event ID | Facility | Old label | New label |")
        lines.append("|----------|----------|-----------|-----------|")
        for e in acled_only:
            lines.append(
                f"| `{e.event_id}` | {e.facility_name} "
                f"| {e.old_label.value} | {e.new_label.value} |"
            )
        lines.append("")
    else:
        lines.append("## Events with ACLED-only corroboration")
        lines.append("")
        lines.append("No events had ACLED-only corroboration.")
        lines.append("")

    # Per-event detail
    lines.append("## Per-event detail")
    lines.append("")
    lines.append(
        "| Event ID | Facility | Old label | New label "
        "| Old p50 | New p50 | S2 fire | ACLED | GDELT |"
    )
    lines.append(
        "|----------|----------|-----------|-----------|"
        "---------|---------|---------|-------|-------|"
    )
    for e in report.events:
        changed = " **changed**" if e.label_changed else ""
        lines.append(
            f"| `{e.event_id}` | {e.facility_name} "
            f"| {e.old_label.value} | {e.new_label.value}{changed} "
            f"| {e.old_p50_tco2e:,.1f} | {e.new_p50_tco2e:,.1f} "
            f"| {'yes' if e.had_s2_fire else 'no'} "
            f"| {'yes' if e.had_acled_corroboration else 'no'} "
            f"| {'yes' if e.had_gdelt_corroboration else 'no'} |"
        )
    lines.append("")

    # Routing note
    lines.append("## Routing")
    lines.append("")
    lines.append(
        "All recomputed events have been routed to `PENDING_REVIEW`. "
        "Each transition is recorded in the `publication_log` table. "
        "No events were auto-published."
    )
    lines.append("")

    return "\n".join(lines)
