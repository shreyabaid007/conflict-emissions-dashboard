"""ProvenanceStore — persistence layer for the provenance DAG.

Three implementations:
  ProvenanceStore          — typing.Protocol; every store must satisfy it
  InMemoryProvenanceStore  — dict-backed; for tests and local development
  PostgresProvenanceStore  — stub; implemented in the database prompt

Nodes are either Source (leaf, no inputs) or ProvenanceRecord (computation).
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable
from uuid import UUID

from wced.models.provenance import ProvenanceChain, ProvenanceRecord, Source


@runtime_checkable
class ProvenanceStore(Protocol):
    """Interface that all provenance store implementations must satisfy.

    @runtime_checkable allows isinstance() checks in tests.
    """

    def record_source(self, source: Source) -> UUID:
        """Persist a Source and return its id."""
        ...

    def record_provenance(self, record: ProvenanceRecord) -> UUID:
        """Persist a ProvenanceRecord and return its id."""
        ...

    def get(self, node_id: UUID) -> ProvenanceRecord | Source:
        """Retrieve a node by id.

        Raises
        ------
        KeyError
            If no node with this id exists in the store.
        """
        ...

    def walk_upstream(self, start_id: UUID) -> Iterator[ProvenanceRecord | Source]:
        """Traverse the DAG upstream from start_id in topological order.

        Yields leaf Sources first; start_id's node is yielded last. Every
        ancestor is yielded exactly once (diamond graphs are handled).

        Raises
        ------
        KeyError
            If start_id or any referenced upstream node is missing.
        ValueError
            If a circular dependency is detected.
        """
        ...


class InMemoryProvenanceStore:
    """Dict-backed provenance store for tests and local development.

    Not thread-safe. State is lost on process restart.
    """

    def __init__(self) -> None:
        self._store: dict[UUID, ProvenanceRecord | Source] = {}

    def record_source(self, source: Source) -> UUID:
        """Persist a Source. Idempotent — re-recording the same id is a no-op."""
        self._store[source.id] = source
        return source.id

    def record_provenance(self, record: ProvenanceRecord) -> UUID:
        """Persist a ProvenanceRecord. Idempotent — re-recording the same id is a no-op."""
        self._store[record.id] = record
        return record.id

    def get(self, node_id: UUID) -> ProvenanceRecord | Source:
        """Retrieve a node by id.

        Raises
        ------
        KeyError
            If no node with this id is in the store.
        """
        try:
            return self._store[node_id]
        except KeyError:
            raise KeyError(f"Provenance node not found: id={node_id}") from None

    def walk_upstream(self, start_id: UUID) -> Iterator[ProvenanceRecord | Source]:
        """Yield all ancestor nodes in topological order, ending with start_id.

        Uses iterative post-order DFS with three-color cycle detection:
          - "white" (unvisited): absent from both visited and in_stack
          - "grey"  (in progress): in in_stack
          - "black" (done): in visited

        Encountering a grey node means we are mid-traversal through it,
        which can only happen via a back-edge → circular dependency.

        Sources are leaf nodes; ProvenanceRecords expand into their inputs.

        Yields
        ------
        ProvenanceRecord | Source
            Topologically ordered: each node appears after all its upstream
            dependencies. start_id's node is always the last item yielded.

        Raises
        ------
        KeyError
            If any referenced node id is absent from the store.
        ValueError
            If a circular dependency is detected in the DAG.
        """
        visited: set[UUID] = set()
        in_stack: set[UUID] = set()
        order: list[UUID] = []

        def _dfs(node_id: UUID) -> None:
            if node_id in in_stack:
                raise ValueError(
                    f"Circular dependency detected in provenance DAG at node {node_id}"
                )
            if node_id in visited:
                return  # already processed via another path (diamond DAG)
            in_stack.add(node_id)
            node = self.get(node_id)  # raises KeyError if absent
            if isinstance(node, ProvenanceRecord):
                for upstream_id in node.inputs:
                    _dfs(upstream_id)
            visited.add(node_id)
            in_stack.discard(node_id)
            order.append(node_id)

        _dfs(start_id)
        for node_id in order:
            yield self._store[node_id]

    def build_chain(self, start_id: UUID) -> ProvenanceChain:
        """Walk upstream from start_id and return a ProvenanceChain."""
        return ProvenanceChain(list(self.walk_upstream(start_id)))

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        return f"InMemoryProvenanceStore(nodes={len(self._store)})"


class PostgresProvenanceStore:
    """PostgreSQL + PostGIS backed provenance store.

    Stub — implemented in the database prompt once SQLAlchemy ORM models
    and Alembic migrations are in place.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def record_source(self, source: Source) -> UUID:
        raise NotImplementedError("PostgresProvenanceStore not yet implemented")

    def record_provenance(self, record: ProvenanceRecord) -> UUID:
        raise NotImplementedError("PostgresProvenanceStore not yet implemented")

    def get(self, node_id: UUID) -> ProvenanceRecord | Source:
        raise NotImplementedError("PostgresProvenanceStore not yet implemented")

    def walk_upstream(self, start_id: UUID) -> Iterator[ProvenanceRecord | Source]:
        raise NotImplementedError("PostgresProvenanceStore not yet implemented")
