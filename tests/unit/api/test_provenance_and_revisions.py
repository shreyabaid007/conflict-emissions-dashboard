"""Tests for the standalone /v1/provenance/{id} and /v1/revisions routes.

Covers v2 gaps 1.5 and C.8:
  - GET /v1/provenance/{id} returns the full upstream source chain.
  - GET /v1/revisions serves the public revision log from publication_log,
    surfacing retractions/restatements and the anomaly "under review" flag.
"""
from __future__ import annotations

from datetime import timezone
from uuid import uuid4

from sqlalchemy import text

from tests.unit.api.conftest import NOW, EVENT_ID, PROVENANCE_ID


def _seed_provenance_chain(db_session) -> str:
    """Attach a Source under the seeded provenance record. Returns source id."""
    source_id = uuid4().hex
    db_session.execute(text(
        "INSERT INTO sources (id, source_type, identifier, retrieved_at, "
        "content_hash, metadata_) VALUES (:id, :st, :idn, :ra, :ch, :md)"
    ), {
        "id": source_id, "st": "SATELLITE",
        "idn": "S2A_MSIL2A_20260305T000000", "ra": NOW.isoformat(),
        "ch": "abc123", "md": "{}",
    })
    db_session.execute(text(
        "INSERT INTO provenance_inputs (provenance_id, input_id, input_type) "
        "VALUES (:pid, :iid, :it)"
    ), {"pid": PROVENANCE_ID, "iid": source_id, "it": "source"})
    db_session.commit()
    return source_id


def _seed_revisions(db_session) -> None:
    rows = [
        ("approve", "PENDING_REVIEW", "PUBLISHED", "publish_gate:auto", None, None),
        ("retract", "PUBLISHED", "RETRACTED", "analyst:jdoe", "Satellite misread", None),
        ("anomaly_retract", "PUBLISHED", "PENDING_REVIEW", "anomaly-watch",
         "p50 is 12.0 robust SDs from the facility median", None),
    ]
    for i, (action, frm, to, actor, reason, mv) in enumerate(rows):
        db_session.execute(text(
            "INSERT INTO publication_log (id, target_type, target_id, from_state, "
            "to_state, action, actor, reason, methodology_version, created_at) "
            "VALUES (:id, :tt, :tid, :frm, :to, :ac, :actor, :rs, :mv, :ca)"
        ), {
            "id": uuid4().hex, "tt": "fire_event", "tid": EVENT_ID,
            "frm": frm, "to": to, "ac": action, "actor": actor, "rs": reason,
            "mv": mv,
            # ascending timestamps so ordering is deterministic
            "ca": NOW.replace(minute=i).isoformat(),
        })
    db_session.commit()


# ---------------------------------------------------------------------------
# /v1/provenance/{id}
# ---------------------------------------------------------------------------


class TestStandaloneProvenance:
    def test_returns_chain_for_provenance_record(self, client, db_session, seed_data) -> None:
        source_id = _seed_provenance_chain(db_session)
        resp = client.get(f"/v1/provenance/{seed_data['provenance_id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["provenance_id"] == seed_data["provenance_id"]
        node_types = {n["node_type"] for n in body["chain"]}
        assert node_types == {"computation", "source"}
        ids = {n["id"] for n in body["chain"]}
        assert str(source_id) in {i.replace("-", "") for i in ids} or source_id in {
            i.replace("-", "") for i in ids
        }
        assert "COMPUTATION" in body["rendered"]

    def test_returns_chain_starting_from_source(self, client, db_session, seed_data) -> None:
        source_id = _seed_provenance_chain(db_session)
        resp = client.get(f"/v1/provenance/{source_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["chain"]) == 1
        assert body["chain"][0]["node_type"] == "source"

    def test_unknown_id_returns_404(self, client, seed_data) -> None:
        resp = client.get(f"/v1/provenance/{uuid4()}")
        assert resp.status_code == 404

    def test_envelope_fields_present(self, client, db_session, seed_data) -> None:
        _seed_provenance_chain(db_session)
        body = client.get(f"/v1/provenance/{seed_data['provenance_id']}").json()
        assert "generated_at" in body
        assert body["data_license"] == "CC-BY 4.0"


# ---------------------------------------------------------------------------
# /v1/revisions
# ---------------------------------------------------------------------------


class TestRevisionLog:
    def test_lists_all_transitions_newest_first(self, client, db_session, seed_data) -> None:
        _seed_revisions(db_session)
        resp = client.get("/v1/revisions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pagination"]["total"] == 3
        actions = [e["action"] for e in body["data"]]
        # newest first → anomaly_retract was inserted last
        assert actions[0] == "anomaly_retract"

    def test_retraction_is_shown_with_reason(self, client, db_session, seed_data) -> None:
        _seed_revisions(db_session)
        body = client.get("/v1/revisions").json()
        retract = next(e for e in body["data"] if e["action"] == "retract")
        assert retract["from_state"] == "PUBLISHED"
        assert retract["to_state"] == "RETRACTED"
        assert retract["reason"] == "Satellite misread"

    def test_anomaly_retract_has_under_review_note(self, client, db_session, seed_data) -> None:
        _seed_revisions(db_session)
        body = client.get("/v1/revisions").json()
        anomaly = next(e for e in body["data"] if e["action"] == "anomaly_retract")
        assert anomaly["public_note"] == "under review"
        assert anomaly["to_state"] == "PENDING_REVIEW"

    def test_non_anomaly_rows_have_no_public_note(self, client, db_session, seed_data) -> None:
        _seed_revisions(db_session)
        body = client.get("/v1/revisions").json()
        approve = next(e for e in body["data"] if e["action"] == "approve")
        assert approve["public_note"] is None

    def test_filter_by_target_id(self, client, db_session, seed_data) -> None:
        _seed_revisions(db_session)
        # An unrelated target should return nothing.
        other = client.get(f"/v1/revisions?target_id={uuid4()}").json()
        assert other["pagination"]["total"] == 0
        mine = client.get(f"/v1/revisions?target_id={seed_data['event_id']}").json()
        assert mine["pagination"]["total"] == 3

    def test_pagination(self, client, db_session, seed_data) -> None:
        _seed_revisions(db_session)
        body = client.get("/v1/revisions?per_page=2&page=1").json()
        assert len(body["data"]) == 2
        assert body["pagination"]["pages"] == 2

    def test_empty_log_returns_empty_data(self, client, seed_data) -> None:
        body = client.get("/v1/revisions").json()
        assert body["data"] == []
        assert body["pagination"]["total"] == 0
