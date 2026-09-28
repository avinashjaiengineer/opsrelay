"""Incident memory, similar-incident retrieval and postmortems."""

import pytest

from opsrelay import memory, postmortem
from opsrelay.runtime.coordinator import handle


def _resolve(service, scenario):
    """Run a scenario to resolution: simulate, approve the proposal."""
    incident = service.simulate(scenario)["incident"]
    if incident["status"] == "awaiting_approval":
        [approval] = service.list_approvals()
        service.decide_approval(approval["id"], approve=True, approver="jane")
    return service.store.get_incident(incident["id"])


def test_closed_incidents_are_remembered(service):
    incident = _resolve(service, "memory-leak")
    assert incident["status"] == "resolved"
    mem = service.store.get_record("memory", incident["id"])
    assert mem["service"] == "auth-service" and mem["category"] == "memory-leak"
    assert (mem["action"], mem["runbook_id"], mem["outcome"]) == ("restart_service", "RB-002", "resolved")
    assert mem["lessons"] and mem["embedding"] and mem["embedder"] == "lexical"
    assert memory.remember(service.store, incident["id"]) is None  # once only


def test_open_incidents_are_not_remembered(service):
    incident = service.simulate("memory-leak")["incident"]
    assert incident["status"] == "awaiting_approval"
    assert memory.remember(service.store, incident["id"]) is None


def test_rejections_are_remembered_and_surface_on_the_next_similar_incident(service, env):
    first = service.simulate("memory-leak")["incident"]
    [approval] = service.list_approvals()
    service.decide_approval(approval["id"], approve=False, approver="jane", note="restart during peak; wait")
    mem = service.store.get_record("memory", first["id"])
    assert mem["outcome"] == "escalated"
    assert mem["rejected"] == [{"action": "restart_service", "by": "jane", "note": "restart during peak; wait"}]

    env.seed(reset=True)
    second = service.simulate("memory-leak")["incident"]
    detail = service.get_incident(second["id"])
    [similar, *_] = detail["similar"]
    assert similar["incident_id"] == first["id"] and similar["similarity"] > 0.5
    proposed = [e for e in detail["events"] if e["kind"] == "remediation.proposed"][-1]
    assert f"restart_service was rejected on {first['id']}" in proposed["message"]


def test_similar_ranks_the_same_failure_first(service, env):
    leak = _resolve(service, "memory-leak")
    env.seed(reset=True)
    spike = _resolve(service, "traffic-spike")
    hits = service.similar_incidents("pods OOMKilled, heap usage 95%, login slow")
    assert hits[0]["incident_id"] == leak["id"]
    hits = service.similar_incidents("CPU saturated during a flash sale, request queue growing")
    assert hits[0]["incident_id"] == spike["id"]
    # A memory leak is not "similar" to a traffic spike: generic words alone don't clear the bar.
    assert service.similar_incidents(incident_id=spike["id"]) == []


def test_backfill_writes_missing_memories(service):
    incident = _resolve(service, "traffic-spike")
    assert service.store.get_record("memory", incident["id"]) is not None
    # A memory lost (e.g. the coordinator stopped before writing it) is backfilled by recover.
    service.store._conn.execute("DELETE FROM records WHERE kind = 'memory'")  # noqa: SLF001
    service.store._conn.commit()  # noqa: SLF001
    assert service.recover()["memories"] == [incident["id"]]
    assert service.recover()["memories"] == []


def test_postmortem_of_a_resolved_incident(service, env):
    earlier = _resolve(service, "memory-leak")
    env.seed(reset=True)
    incident = _resolve(service, "memory-leak")
    doc = postmortem.render(service.store, incident["id"])
    assert not doc["generated"]
    assert doc["markdown"].startswith(f"# {incident['id']} Postmortem")
    assert "## Similar past incidents" in doc["markdown"] and earlier["id"] in doc["markdown"]
    assert "Audit chain: intact" in doc["markdown"]


def test_postmortem_draft_for_an_escalated_incident(service):
    service.simulate("bad-deploy")
    [approval] = service.list_approvals()
    service.decide_approval(approval["id"], approve=False, approver="sam", note="release owner is fixing forward")
    doc = service.postmortem(approval["incident_id"])
    assert doc["generated"] and "Draft generated from the incident record" in doc["markdown"]
    assert "rejected by sam, note: release owner is fixing forward" in doc["markdown"]


def test_postmortem_waits_until_the_incident_is_closed(service):
    incident = service.simulate("bad-deploy")["incident"]
    with pytest.raises(ValueError, match="once it is closed"):
        service.postmortem(incident["id"])


def test_api_actions(service, monkeypatch):
    from opsrelay.runtime import coordinator

    monkeypatch.setattr(coordinator, "_service", service)
    incident = _resolve(service, "memory-leak")
    assert handle({"action": "get_postmortem", "incident_id": incident["id"]})["markdown"]
    assert handle({"action": "similar_incidents", "query": "OOMKilled"})["similar"][0]["incident_id"] == incident["id"]
    assert handle({"action": "recover"})["memories"] == []
