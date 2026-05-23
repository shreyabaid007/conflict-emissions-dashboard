"""Tests for provenance models and InMemoryProvenanceStore.

Covers:
- Source and ProvenanceRecord construction and field validation
- record / retrieve round-trip
- walk_upstream topological ordering: linear chain, branching DAG, diamond DAG
- Circular dependency detection: self-loop, two-node cycle, three-node cycle
- ProvenanceChain properties and rendering
- Protocol compliance for InMemoryProvenanceStore and PostgresProvenanceStore
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from wced.models.provenance import (
    ConfidenceLabel,
    ProvenanceChain,
    ProvenanceRecord,
    Source,
    SourceType,
)
from wced.provenance.store import (
    InMemoryProvenanceStore,
    PostgresProvenanceStore,
    ProvenanceStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_source(**kwargs: object) -> Source:
    defaults: dict[str, object] = dict(
        source_type=SourceType.SATELLITE,
        identifier="S2A_MSIL2A_20260301T000000_N0510_R108_T38SMB",
        retrieved_at=utcnow(),
        retrieved_by="wced.ingest.sentinel2",
        content_hash="a" * 64,
        metadata={"platform": "Sentinel-2A", "cloud_cover": 0.03},
    )
    return Source(**(defaults | kwargs))  # type: ignore[arg-type]


def make_record(**kwargs: object) -> ProvenanceRecord:
    defaults: dict[str, object] = dict(
        produced_by="wced.quantify.frp",
        inputs=[],
        method="frp_to_co2_v1.0",
        parameters={"n_samples": 10_000, "rng_seed": 42},
        produced_at=utcnow(),
        confidence_label=ConfidenceLabel.REPORTED,
        notes=None,
    )
    return ProvenanceRecord(**(defaults | kwargs))  # type: ignore[arg-type]


def store_all(store: InMemoryProvenanceStore, *nodes: ProvenanceRecord | Source) -> None:
    for node in nodes:
        if isinstance(node, Source):
            store.record_source(node)
        else:
            store.record_provenance(node)


# ---------------------------------------------------------------------------
# Source model
# ---------------------------------------------------------------------------


class TestSourceModel:
    def test_default_id_is_uuid(self) -> None:
        s = make_source()
        assert isinstance(s.id, UUID)

    def test_explicit_id_preserved(self) -> None:
        fixed = uuid4()
        s = make_source(id=fixed)
        assert s.id == fixed

    def test_all_source_types_accepted(self) -> None:
        for st in SourceType:
            s = make_source(source_type=st)
            assert s.source_type == st

    def test_frozen_rejects_mutation(self) -> None:
        s = make_source()
        with pytest.raises((ValidationError, TypeError)):
            s.identifier = "mutated"  # type: ignore[misc]

    def test_metadata_defaults_empty_dict(self) -> None:
        s = Source(
            source_type=SourceType.NEWS,
            identifier="https://reuters.com/article/123",
            retrieved_at=utcnow(),
            retrieved_by="analyst:jdoe",
            content_hash="b" * 64,
        )
        assert s.metadata == {}

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            make_source(retrieved_at=datetime(2026, 3, 1, 12, 0, 0))  # no tzinfo


# ---------------------------------------------------------------------------
# ProvenanceRecord model
# ---------------------------------------------------------------------------


class TestProvenanceRecordModel:
    def test_default_id_is_uuid(self) -> None:
        r = make_record()
        assert isinstance(r.id, UUID)

    def test_inputs_defaults_empty(self) -> None:
        r = make_record()
        assert r.inputs == []

    def test_all_confidence_labels_accepted(self) -> None:
        for cl in ConfidenceLabel:
            r = make_record(confidence_label=cl)
            assert r.confidence_label == cl

    def test_frozen_rejects_mutation(self) -> None:
        r = make_record()
        with pytest.raises((ValidationError, TypeError)):
            r.method = "mutated"  # type: ignore[misc]

    def test_notes_optional(self) -> None:
        assert make_record(notes=None).notes is None
        assert make_record(notes="fallback used").notes == "fallback used"

    def test_parameters_defaults_empty_dict(self) -> None:
        r = ProvenanceRecord(
            produced_by="wced.quantify.frp",
            method="frp_to_co2_v1.0",
            produced_at=utcnow(),
            confidence_label=ConfidenceLabel.REPORTED,
        )
        assert r.parameters == {}


# ---------------------------------------------------------------------------
# InMemoryProvenanceStore — record / retrieve round-trip
# ---------------------------------------------------------------------------


class TestStoreRoundtrip:
    def test_record_source_returns_id(self) -> None:
        s = make_source()
        returned = InMemoryProvenanceStore().record_source(s)
        assert returned == s.id

    def test_record_provenance_returns_id(self) -> None:
        r = make_record()
        returned = InMemoryProvenanceStore().record_provenance(r)
        assert returned == r.id

    def test_get_source_roundtrip(self) -> None:
        store = InMemoryProvenanceStore()
        s = make_source()
        store.record_source(s)
        assert store.get(s.id) == s

    def test_get_record_roundtrip(self) -> None:
        store = InMemoryProvenanceStore()
        r = make_record()
        store.record_provenance(r)
        assert store.get(r.id) == r

    def test_get_missing_raises_key_error(self) -> None:
        store = InMemoryProvenanceStore()
        missing = uuid4()
        with pytest.raises(KeyError):
            store.get(missing)

    def test_len_counts_both_types(self) -> None:
        store = InMemoryProvenanceStore()
        assert len(store) == 0
        store.record_source(make_source())
        assert len(store) == 1
        store.record_provenance(make_record())
        assert len(store) == 2

    def test_record_is_idempotent(self) -> None:
        store = InMemoryProvenanceStore()
        s = make_source()
        store.record_source(s)
        store.record_source(s)  # same id — must not duplicate
        assert len(store) == 1


# ---------------------------------------------------------------------------
# walk_upstream — topological ordering
# ---------------------------------------------------------------------------


class TestWalkUpstream:
    def test_single_source(self) -> None:
        store = InMemoryProvenanceStore()
        s = make_source()
        store.record_source(s)
        assert list(store.walk_upstream(s.id)) == [s]

    def test_single_record_no_inputs(self) -> None:
        store = InMemoryProvenanceStore()
        r = make_record()
        store.record_provenance(r)
        assert list(store.walk_upstream(r.id)) == [r]

    def test_linear_chain_yields_sources_first(self) -> None:
        """S → R1 → R2: walk from R2 must yield [S, R1, R2]."""
        store = InMemoryProvenanceStore()
        s = make_source()
        r1 = make_record(inputs=[s.id])
        r2 = make_record(inputs=[r1.id])
        store_all(store, s, r1, r2)

        chain = list(store.walk_upstream(r2.id))
        assert len(chain) == 3
        assert chain[0] == s
        assert chain[1] == r1
        assert chain[2] == r2

    def test_branching_dag_topological_order(self) -> None:
        """
        S1  S2
         \\  /
          R1
          |
          R2
        Both sources must appear before R1; R1 must appear before R2.
        """
        store = InMemoryProvenanceStore()
        s1 = make_source(identifier="source-1")
        s2 = make_source(identifier="source-2")
        r1 = make_record(inputs=[s1.id, s2.id])
        r2 = make_record(inputs=[r1.id])
        store_all(store, s1, s2, r1, r2)

        chain = list(store.walk_upstream(r2.id))
        assert len(chain) == 4

        ids = [n.id for n in chain]
        assert ids.index(s1.id) < ids.index(r1.id)
        assert ids.index(s2.id) < ids.index(r1.id)
        assert ids.index(r1.id) < ids.index(r2.id)
        assert chain[-1] == r2

    def test_diamond_dag_visits_shared_node_once(self) -> None:
        """
        S
        |\\
        R1 R2
        |/
        R3
        S must appear exactly once despite being reachable via two paths.
        """
        store = InMemoryProvenanceStore()
        s = make_source()
        r1 = make_record(inputs=[s.id])
        r2 = make_record(inputs=[s.id])
        r3 = make_record(inputs=[r1.id, r2.id])
        store_all(store, s, r1, r2, r3)

        chain = list(store.walk_upstream(r3.id))
        node_ids = [n.id for n in chain]

        assert len(chain) == 4  # S visited exactly once
        assert node_ids.count(s.id) == 1
        assert node_ids.index(s.id) < node_ids.index(r1.id)
        assert node_ids.index(s.id) < node_ids.index(r2.id)
        assert node_ids.index(r1.id) < node_ids.index(r3.id)
        assert node_ids.index(r2.id) < node_ids.index(r3.id)

    def test_missing_upstream_node_raises_key_error(self) -> None:
        store = InMemoryProvenanceStore()
        ghost = uuid4()
        r = make_record(inputs=[ghost])
        store.record_provenance(r)
        with pytest.raises(KeyError):
            list(store.walk_upstream(r.id))

    def test_walk_start_id_not_in_store_raises_key_error(self) -> None:
        store = InMemoryProvenanceStore()
        with pytest.raises(KeyError):
            list(store.walk_upstream(uuid4()))


# ---------------------------------------------------------------------------
# Circular dependency detection
# ---------------------------------------------------------------------------


class TestCircularDependency:
    def test_self_loop_raises(self) -> None:
        store = InMemoryProvenanceStore()
        r_id = uuid4()
        store.record_provenance(make_record(id=r_id, inputs=[r_id]))
        with pytest.raises(ValueError, match="[Cc]ircular"):
            list(store.walk_upstream(r_id))

    def test_two_node_cycle_raises(self) -> None:
        store = InMemoryProvenanceStore()
        id_a, id_b = uuid4(), uuid4()
        store.record_provenance(make_record(id=id_a, inputs=[id_b]))
        store.record_provenance(make_record(id=id_b, inputs=[id_a]))
        with pytest.raises(ValueError, match="[Cc]ircular"):
            list(store.walk_upstream(id_a))

    def test_three_node_cycle_raises(self) -> None:
        store = InMemoryProvenanceStore()
        id_a, id_b, id_c = uuid4(), uuid4(), uuid4()
        store.record_provenance(make_record(id=id_a, inputs=[id_b]))
        store.record_provenance(make_record(id=id_b, inputs=[id_c]))
        store.record_provenance(make_record(id=id_c, inputs=[id_a]))
        with pytest.raises(ValueError, match="[Cc]ircular"):
            list(store.walk_upstream(id_a))

    def test_valid_dag_does_not_raise(self) -> None:
        """Regression: a diamond DAG must not be mistaken for a cycle."""
        store = InMemoryProvenanceStore()
        s = make_source()
        r1 = make_record(inputs=[s.id])
        r2 = make_record(inputs=[s.id])
        r3 = make_record(inputs=[r1.id, r2.id])
        store_all(store, s, r1, r2, r3)
        # Must not raise
        chain = list(store.walk_upstream(r3.id))
        assert len(chain) == 4


# ---------------------------------------------------------------------------
# ProvenanceChain
# ---------------------------------------------------------------------------


class TestProvenanceChain:
    def _build(self) -> tuple[
        InMemoryProvenanceStore, Source, ProvenanceRecord, ProvenanceRecord
    ]:
        store = InMemoryProvenanceStore()
        s = make_source(source_type=SourceType.SATELLITE, identifier="sentinel-2-scene-001")
        r1 = make_record(
            inputs=[s.id],
            method="frp_to_co2_v1.0",
            confidence_label=ConfidenceLabel.VERIFIED,
        )
        r2 = make_record(
            inputs=[r1.id],
            method="monte_carlo_v1.0",
            confidence_label=ConfidenceLabel.REPORTED,
        )
        store_all(store, s, r1, r2)
        return store, s, r1, r2

    def test_build_chain_returns_provenance_chain(self) -> None:
        store, _s, _r1, r2 = self._build()
        chain = store.build_chain(r2.id)
        assert isinstance(chain, ProvenanceChain)

    def test_len_matches_node_count(self) -> None:
        store, _s, _r1, r2 = self._build()
        assert len(store.build_chain(r2.id)) == 3

    def test_sources_property(self) -> None:
        store, s, _r1, r2 = self._build()
        chain = store.build_chain(r2.id)
        assert chain.sources == [s]

    def test_records_property(self) -> None:
        store, _s, r1, r2 = self._build()
        chain = store.build_chain(r2.id)
        assert chain.records == [r1, r2]

    def test_root_is_terminal_node(self) -> None:
        store, _s, _r1, r2 = self._build()
        assert store.build_chain(r2.id).root == r2

    def test_empty_chain_root_is_none(self) -> None:
        assert ProvenanceChain([]).root is None

    def test_confidence_weakest_link(self) -> None:
        # r1=VERIFIED, r2=REPORTED → chain confidence is REPORTED
        store, _s, _r1, r2 = self._build()
        assert store.build_chain(r2.id).confidence == ConfidenceLabel.REPORTED

    def test_confidence_all_confirmed(self) -> None:
        store = InMemoryProvenanceStore()
        r = make_record(confidence_label=ConfidenceLabel.CONFIRMED)
        store.record_provenance(r)
        assert store.build_chain(r.id).confidence == ConfidenceLabel.CONFIRMED

    def test_confidence_claimed_is_weakest(self) -> None:
        store = InMemoryProvenanceStore()
        r1 = make_record(confidence_label=ConfidenceLabel.CONFIRMED)
        r2 = make_record(inputs=[r1.id], confidence_label=ConfidenceLabel.CLAIMED)
        store_all(store, r1, r2)
        assert store.build_chain(r2.id).confidence == ConfidenceLabel.CLAIMED

    def test_empty_chain_confidence_is_none(self) -> None:
        assert ProvenanceChain([]).confidence is None

    def test_render_includes_source_identifiers(self) -> None:
        store, _s, _r1, r2 = self._build()
        rendered = store.build_chain(r2.id).render()
        assert "SATELLITE" in rendered
        assert "sentinel-2-scene-001" in rendered

    def test_render_includes_method_names(self) -> None:
        store, _s, _r1, r2 = self._build()
        rendered = store.build_chain(r2.id).render()
        assert "frp_to_co2_v1.0" in rendered
        assert "monte_carlo_v1.0" in rendered

    def test_render_includes_confidence_labels(self) -> None:
        store, _s, _r1, r2 = self._build()
        rendered = store.build_chain(r2.id).render()
        assert "VERIFIED" in rendered
        assert "REPORTED" in rendered

    def test_render_includes_notes_when_present(self) -> None:
        store = InMemoryProvenanceStore()
        r = make_record(notes="fallback path used — VIDA footprint missing")
        store.record_provenance(r)
        assert "fallback path used" in store.build_chain(r.id).render()

    def test_render_omits_note_line_when_none(self) -> None:
        store = InMemoryProvenanceStore()
        r = make_record(notes=None)
        store.record_provenance(r)
        assert "note:" not in store.build_chain(r.id).render()


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_in_memory_satisfies_protocol(self) -> None:
        assert isinstance(InMemoryProvenanceStore(), ProvenanceStore)

    def test_postgres_stub_satisfies_protocol(self) -> None:
        assert isinstance(
            PostgresProvenanceStore("postgresql://localhost/wced"), ProvenanceStore
        )

    def test_postgres_record_source_raises(self) -> None:
        store = PostgresProvenanceStore("postgresql://localhost/wced")
        with pytest.raises(NotImplementedError):
            store.record_source(make_source())

    def test_postgres_record_provenance_raises(self) -> None:
        store = PostgresProvenanceStore("postgresql://localhost/wced")
        with pytest.raises(NotImplementedError):
            store.record_provenance(make_record())

    def test_postgres_get_raises(self) -> None:
        store = PostgresProvenanceStore("postgresql://localhost/wced")
        with pytest.raises(NotImplementedError):
            store.get(uuid4())

    def test_postgres_walk_upstream_raises(self) -> None:
        store = PostgresProvenanceStore("postgresql://localhost/wced")
        with pytest.raises(NotImplementedError):
            list(store.walk_upstream(uuid4()))
