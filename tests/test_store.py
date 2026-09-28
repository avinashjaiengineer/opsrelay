"""The same contract for the SQLite (local) and DynamoDB (AWS) stores."""

import threading

import boto3
import pytest
from moto import mock_aws

from opsrelay.audit import verify_chain
from opsrelay.store import new_approval_id, new_incident_id, now_iso
from opsrelay.store.base import GENESIS_HASH, StatusChangeError
from opsrelay.store.dynamodb import DynamoStore
from opsrelay.store.sqlite import SqliteStore


@pytest.fixture(params=["sqlite", "dynamodb"])
def any_store(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        yield SqliteStore(str(tmp_path / "s.db"))
        return
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "testing")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        DynamoStore.create_table("opsrelay-test", resource=resource)
        yield DynamoStore("opsrelay-test", resource=resource)


def _incident(**kw):
    ts = now_iso()
    return {"id": new_incident_id(), "title": "t", "status": "open", "created_at": ts, "updated_at": ts, **kw}


def _approval(incident_id, **kw):
    return {
        "id": new_approval_id(),
        "incident_id": incident_id,
        "action": "restart_service",
        "service": "auth-service",
        "status": "pending",
        "created_at": now_iso(),
        **kw,
    }


def test_incidents_round_trip_newest_first(any_store):
    a = _incident(title="first", created_at="2026-01-01T00:00:00+00:00")
    b = _incident(title="second", created_at="2026-01-02T00:00:00+00:00")
    any_store.put_incident(a)
    any_store.put_incident(b)
    assert any_store.get_incident(a["id"])["title"] == "first"
    assert [i["title"] for i in any_store.list_incidents()] == ["second", "first"]
    updated = any_store.update_incident(a["id"], score=0.5)
    assert any_store.get_incident(a["id"])["score"] == 0.5 == updated["score"]  # floats survive DynamoDB
    assert any_store.get_incident("inc-missing") is None


def test_status_changes_only_by_compare_and_set(any_store):
    inc = _incident()
    any_store.put_incident(inc)
    with pytest.raises(StatusChangeError):
        any_store.update_incident(inc["id"], status="resolved")
    assert any_store.transition_incident(inc["id"], "triaging", {"status": "investigating"}) is None
    moved = any_store.transition_incident(inc["id"], "open", {"status": "triaging"})
    assert moved["status"] == any_store.get_incident(inc["id"])["status"] == "triaging"
    # A second writer that still thinks the incident is open loses.
    assert any_store.transition_incident(inc["id"], "open", {"status": "escalated"}) is None


def test_events_are_ordered_and_hash_chained_per_incident(any_store):
    inc = _incident()
    any_store.put_incident(inc)
    for i in range(5):
        any_store.record(inc["id"], "tester", "note", f"event {i}", {"i": i}, input={"i": i})
    any_store.record("inc-other00000", "tester", "note", "elsewhere")
    events = any_store.list_events(inc["id"])
    assert [e["data"]["i"] for e in events] == [0, 1, 2, 3, 4]
    assert events[0]["prev_hash"] == GENESIS_HASH
    assert all(b["prev_hash"] == a["hash"] for a, b in zip(events, events[1:], strict=False))
    assert events[0]["input_hash"] and events[0]["actor_type"] == "human"
    assert verify_chain(events)["ok"]


def test_tampering_with_an_event_is_detected(any_store):
    inc = _incident()
    any_store.put_incident(inc)
    for i in range(4):
        any_store.record(inc["id"], "platform", "note", f"event {i}")
    events = any_store.list_events(inc["id"])

    edited = [dict(e) for e in events]
    edited[1]["message"] = "nothing happened here"
    result = verify_chain(edited)
    assert (result["ok"], result["at"], result["event_id"]) == (False, 1, events[1]["id"])
    assert verify_chain([events[0], *events[2:]])["at"] == 1  # a deleted event breaks the link
    assert verify_chain(events)["ok"]


def test_executions_are_claimed_once(any_store):
    assert any_store.claim_execution({"key": "inc-1:restart:svc:x", "state": "running"}) is True
    assert any_store.claim_execution({"key": "inc-1:restart:svc:x", "state": "running"}) is False
    done = any_store.finish_execution("inc-1:restart:svc:x", {"state": "done", "result": {"ok": True}})
    assert done["result"] == {"ok": True} == any_store.get_execution("inc-1:restart:svc:x")["result"]


def test_dead_letters_newest_first(any_store):
    any_store.put_dead_letter({"id": "dlq-1", "created_at": "2026-01-01T00:00:00+00:00", "agent": "triage"})
    any_store.put_dead_letter({"id": "dlq-2", "created_at": "2026-01-02T00:00:00+00:00", "agent": "diagnostics"})
    assert [d["id"] for d in any_store.list_dead_letters()] == ["dlq-2", "dlq-1"]


def test_approval_transition_is_conditional(any_store):
    inc = _incident()
    any_store.put_incident(inc)
    approval = _approval(inc["id"])
    any_store.put_approval(approval)

    assert any_store.list_approvals(status="pending", incident_id=inc["id"])[0]["id"] == approval["id"]
    first = any_store.transition_approval(approval["id"], "pending", {"status": "approved", "decided_by": "a"})
    second = any_store.transition_approval(approval["id"], "pending", {"status": "rejected", "decided_by": "b"})
    assert first["status"] == "approved"
    assert second is None
    stored = any_store.get_approval(approval["id"])
    assert (stored["status"], stored["decided_by"]) == ("approved", "a")
    assert any_store.list_approvals(status="pending") == []


def test_services(any_store):
    any_store.put_service({"name": "b-svc", "tier": 2})
    any_store.put_service({"name": "a-svc", "tier": 1})
    assert [s["name"] for s in any_store.list_services()] == ["a-svc", "b-svc"]
    assert any_store.get_service("a-svc")["tier"] == 1


def test_sqlite_only_one_concurrent_decision_wins(tmp_path):
    store = SqliteStore(str(tmp_path / "race.db"))
    inc = _incident()
    store.put_incident(inc)
    approval = _approval(inc["id"])
    store.put_approval(approval)
    winners = []

    def decide(who):
        if store.transition_approval(approval["id"], "pending", {"status": "approved", "decided_by": who}):
            winners.append(who)

    threads = [threading.Thread(target=decide, args=(f"user{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_sqlite_concurrent_writers_keep_one_chain(tmp_path):
    store = SqliteStore(str(tmp_path / "chain.db"))
    inc = _incident()
    store.put_incident(inc)
    threads = [
        threading.Thread(
            target=lambda n=n: [store.record(inc["id"], "platform", "note", f"{n}-{i}") for i in range(10)]
        )
        for n in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = store.list_events(inc["id"])
    assert len(events) == 50
    assert verify_chain(events)["ok"]
