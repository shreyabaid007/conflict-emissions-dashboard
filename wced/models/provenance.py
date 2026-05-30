"""Provenance data models.

Every number in WCED traces to a ProvenanceRecord that lists its upstream
Sources and the algorithm that produced it. Together they form a directed
acyclic graph (DAG) that can be walked from any emission estimate back to the
raw satellite images or news reports that seeded it.

Placeholder reference: methodology/v1.0.pdf §1 — "Data Provenance and Source
Attribution". (PDF pending Scientific Steering Committee approval.)
"""
from __future__ import annotations

import enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class SourceType(str, enum.Enum):
    """Classification of an atomic information source."""

    SATELLITE = "SATELLITE"
    NEWS = "NEWS"
    OFFICIAL_STATEMENT = "OFFICIAL_STATEMENT"
    ACLED = "ACLED"
    GDELT = "GDELT"
    NGO_REPORT = "NGO_REPORT"
    ACADEMIC_PAPER = "ACADEMIC_PAPER"
    DERIVED = "DERIVED"  # output of a computation step, not a raw source


class ConfidenceLabel(str, enum.Enum):
    """Evidential confidence in a ProvenanceRecord's output.

    Maps to the editorial workflow: incidents enter as REPORTED and may be
    upgraded as corroborating sources arrive. Order from strongest to weakest:
    CONFIRMED > VERIFIED > REPORTED > SUSPECTED > CLAIMED.

    CONFIRMED  — satellite evidence + ≥2 independent non-official sources
    VERIFIED   — ≥2 independent sources (at least one non-official)
    REPORTED   — ≥1 credible source with no known contradiction
    SUSPECTED  — circumstantial evidence only (spatial/temporal proximity)
    CLAIMED    — only official or partisan statements; treat as unverified
    """

    CONFIRMED = "CONFIRMED"
    VERIFIED = "VERIFIED"
    REPORTED = "REPORTED"
    SUSPECTED = "SUSPECTED"
    CLAIMED = "CLAIMED"


class Source(BaseModel):
    """An atomic information source: satellite image, news article, or report.

    Sources are leaf nodes in the provenance DAG — they have no upstream
    inputs. A single raw file (e.g. one Sentinel-2 scene) should produce
    exactly one Source record so the same data is never double-counted.

    Parameters
    ----------
    id : UUID
        Stable identifier. Pre-generate and store with the artefact so
        re-ingesting the same file returns the same ID.
    source_type : SourceType
        Broad classification of the information source.
    identifier : str
        Canonical reference: a URL, DOI, Sentinel product ID, or similar.
    retrieved_at : AwareDatetime
        When this source was fetched (always UTC).
    retrieved_by : str
        System component or human operator that performed the retrieval,
        e.g. "wced.ingest.sentinel2" or "analyst:jdoe".
    content_hash : str
        SHA-256 hex digest of the raw bytes at retrieval time. Used to
        detect accidental modification of cached artefacts.
    metadata : dict[str, Any]
        Source-specific fields. For satellite: bbox, platform, cloud_cover.
        For news: author, outlet, language. Never None; use empty dict.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    source_type: SourceType
    identifier: str
    retrieved_at: AwareDatetime
    retrieved_by: str
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProvenanceRecord(BaseModel):
    """A single computation step in the provenance DAG.

    Every function that produces a Distribution must create a ProvenanceRecord
    that captures what inputs it consumed, which algorithm it used, and all
    parameters needed to reproduce the output.

    Parameters
    ----------
    id : UUID
        Stable identifier for this computation step.
    produced_by : str
        Fully-qualified Python module path, e.g. "wced.quantify.frp".
    inputs : list[UUID]
        IDs of upstream ProvenanceRecords or Sources consumed by this step.
        Empty only for records that directly wrap a raw source ingestion.
    method : str
        Algorithm name and version string, e.g. "frp_to_co2_v1.0". Must
        correspond to a section of methodology/v1.0.pdf.
    parameters : dict[str, Any]
        All parameters used in the computation: factor values, thresholds,
        n_samples, rng seed. Must be sufficient to reproduce the output
        given the same inputs.
    produced_at : AwareDatetime
        Wall-clock time when this computation ran (always UTC).
    confidence_label : ConfidenceLabel
        Evidential confidence in this record's output. The weakest label
        in the full upstream chain governs what tier the estimate reaches.
    notes : str or None
        Optional free-text annotation, e.g. "fallback to GHSL volume
        estimate — VIDA footprint missing for this tile".
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    produced_by: str
    inputs: list[UUID] = Field(default_factory=list)
    method: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    produced_at: AwareDatetime
    confidence_label: ConfidenceLabel
    notes: str | None = None


# Ordered from strongest to weakest for ProvenanceChain.confidence.
_CONFIDENCE_ORDER: list[ConfidenceLabel] = [
    ConfidenceLabel.CONFIRMED,
    ConfidenceLabel.VERIFIED,
    ConfidenceLabel.REPORTED,
    ConfidenceLabel.SUSPECTED,
    ConfidenceLabel.CLAIMED,
]


class ProvenanceChain:
    """An ordered traversal of the provenance DAG for one emission estimate.

    Nodes are in topological order: leaf Sources first (raw data), the
    terminal ProvenanceRecord last (the estimate being audited). Produced by
    ``InMemoryProvenanceStore.build_chain()`` or ``walk_upstream()``.

    Parameters
    ----------
    nodes : list[ProvenanceRecord | Source]
        Topologically-ordered sequence of all nodes from raw sources through
        to the emission estimate's immediate ProvenanceRecord.
    """

    def __init__(self, nodes: list[ProvenanceRecord | Source]) -> None:
        self.nodes = nodes

    @property
    def sources(self) -> list[Source]:
        """All leaf Sources in this chain (raw data inputs)."""
        return [n for n in self.nodes if isinstance(n, Source)]

    @property
    def records(self) -> list[ProvenanceRecord]:
        """All computation records in this chain."""
        return [n for n in self.nodes if isinstance(n, ProvenanceRecord)]

    @property
    def root(self) -> ProvenanceRecord | Source | None:
        """Terminal node — the estimate being audited. None if chain is empty."""
        return self.nodes[-1] if self.nodes else None

    @property
    def confidence(self) -> ConfidenceLabel | None:
        """Weakest-link confidence across all records in the chain.

        An estimate is only as trustworthy as its least-confident computation
        step. Returns None if there are no ProvenanceRecords in the chain.
        """
        if not self.records:
            return None
        labels = {r.confidence_label for r in self.records}
        # Iterate weakest → strongest; return the first match found.
        for label in reversed(_CONFIDENCE_ORDER):
            if label in labels:
                return label
        return self.records[-1].confidence_label  # unreachable in practice

    def render(self, indent: int = 2) -> str:
        """Render the chain as a human-readable audit trail string."""
        pad = " " * indent
        lines: list[str] = []
        for i, node in enumerate(self.nodes):
            arrow = "→ " if i > 0 else "  "
            if isinstance(node, Source):
                lines.append(
                    f"{arrow}[{node.source_type.value}] {node.identifier}"
                    f" (retrieved {node.retrieved_at.isoformat()}"
                    f" by {node.retrieved_by})"
                )
            else:
                note_line = f"\n{pad}  note: {node.notes}" if node.notes else ""
                lines.append(
                    f"{arrow}[COMPUTATION] {node.produced_by} / {node.method}"
                    f" [{node.confidence_label.value}]{note_line}"
                )
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.nodes)

    def __repr__(self) -> str:
        return (
            f"ProvenanceChain(nodes={len(self.nodes)}, confidence={self.confidence})"
        )
