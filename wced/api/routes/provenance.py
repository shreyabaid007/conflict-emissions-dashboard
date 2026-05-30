"""Standalone provenance endpoint.

``GET /v1/provenance/{id}`` returns the full upstream source chain for any
provenance-record or source id, so every number rendered on the dashboard is
click-through-auditable without going through an event (v2 §6, gap C.8).

The walk mirrors ``events.get_event_provenance`` but starts from an arbitrary
node id rather than an event's latest estimate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from wced.api.dependencies import DbSession
from wced.api.schemas.responses import ProvenanceChainResponse, ProvenanceNodeOut
from wced.db import models

router = APIRouter(prefix="/v1/provenance", tags=["provenance"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def walk_provenance(
    db: DbSession, start_id: UUID
) -> tuple[list[ProvenanceNodeOut], list[str]]:
    """Breadth-first walk of the provenance DAG from *start_id*.

    Returns the visited nodes (computations and sources) and a list of rendered
    one-line summaries. Diamond graphs are de-duplicated via a visited set.
    Returns ``([], [])`` when *start_id* matches no node.
    """
    chain_nodes: list[ProvenanceNodeOut] = []
    rendered_lines: list[str] = []
    visited: set[UUID] = set()
    queue: list[UUID] = [start_id]

    while queue:
        current_id = queue.pop(0)
        if current_id in visited:
            continue
        visited.add(current_id)

        rec = db.execute(
            select(models.provenance_records).where(
                models.provenance_records.c.id == current_id
            )
        ).first()
        if rec:
            rec_dict = rec._asdict()
            chain_nodes.append(ProvenanceNodeOut(
                node_type="computation",
                id=rec_dict["id"],
                detail={
                    "produced_by": rec_dict["produced_by"],
                    "method": rec_dict["method"],
                    "confidence_label": rec_dict["confidence_label"],
                    "produced_at": rec_dict["produced_at"].isoformat()
                    if rec_dict["produced_at"] else None,
                    "notes": rec_dict.get("notes"),
                },
            ))
            rendered_lines.append(
                f"[COMPUTATION] {rec_dict['produced_by']} / {rec_dict['method']}"
                f" [{rec_dict['confidence_label']}]"
            )
            inputs = db.execute(
                select(models.provenance_inputs).where(
                    models.provenance_inputs.c.provenance_id == current_id
                )
            ).all()
            for inp in inputs:
                queue.append(inp._asdict()["input_id"])
            continue

        src = db.execute(
            select(models.sources).where(models.sources.c.id == current_id)
        ).first()
        if src:
            src_dict = src._asdict()
            chain_nodes.append(ProvenanceNodeOut(
                node_type="source",
                id=src_dict["id"],
                detail={
                    "source_type": src_dict["source_type"],
                    "identifier": src_dict["identifier"],
                    "retrieved_at": src_dict["retrieved_at"].isoformat()
                    if src_dict["retrieved_at"] else None,
                },
            ))
            rendered_lines.append(
                f"[{src_dict['source_type']}] {src_dict['identifier']}"
            )

    return chain_nodes, rendered_lines


@router.get(
    "/{provenance_id}",
    response_model=ProvenanceChainResponse,
    responses={404: {"description": "Provenance node not found"}},
)
def get_provenance(provenance_id: UUID, db: DbSession) -> ProvenanceChainResponse:
    """Full upstream source chain for a provenance-record or source id."""
    chain, rendered = walk_provenance(db, provenance_id)
    if not chain:
        raise HTTPException(
            404, detail=f"Provenance node {provenance_id} not found"
        )
    return ProvenanceChainResponse(
        generated_at=_now(),
        provenance_id=provenance_id,
        chain=chain,
        rendered="\n→ ".join(rendered),
    )
