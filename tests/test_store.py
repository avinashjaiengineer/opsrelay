"""The same contract for the SQLite (local) and DynamoDB (AWS) stores."""

import threading

import boto3
import pytest
from moto import mock_aws

from agentmesh.store import new_approval_id, new_incident_id, now_iso
from agentmesh.store.dynamodb import DynamoStore
from agentmesh.store.sqlite import SqliteStore


@pytest.fixture(params=["sqlite", "dynamodb"])
def any_store(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        yield SqliteStore(str(tmp_path / "s.db"))
        return
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "testing")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        DynamoStore.create_table("agentmesh-test", resource=resource)
        yield DynamoStore("agentmesh-test", resource=resource)


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
    updated = any_store.update_incident(a["id"], status="resolved", score=0.5)
    assert any_store.get_incident(a["id"])["status"] == "resolved" == updated["status"]
    assert any_store.get_incident(a["id"])["score"] == 0.5  # floats survive DynamoDB
    assert any_store.get_incident("inc-missing") is None


def test_events_are_ordered_per_incident(any_store):
    inc = _incident()
    any_store.put_incident(inc)
    for i in range(5):
        any_store.record(inc["id"], "tester", "note", f"event {i}", {"i": i})
    any_store.record("inc-other00000", "tester", "note", "elsewhere")
    events = any_store.list_events(inc["id"])
    assert [e["data"]["i"] for e in events] == [0, 1, 2, 3, 4]


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
