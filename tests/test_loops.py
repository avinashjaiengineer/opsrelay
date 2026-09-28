"""Loop breaking and injection isolation, found by the Nova eval."""

from opsrelay import offline
from opsrelay.agents import factory
from opsrelay.config import SPECIALISTS
from opsrelay.offline import Call
from opsrelay.service import IncidentService


def test_a_repeated_failing_call_is_refused(service, monkeypatch):
    incident = service.simulate("traffic-spike", run=False)["incident"]
    iid = incident["id"]
    bad = {
        "incident_id": iid,
        "root_cause": "CPU saturation",
        "category": "saturation",
        "evidence": [{"source": "guesswork", "value": "cpu 96%"}],  # not an evidence source
        "confidence": 0.9,
        "affected_component": "inventory-service",
        "recommended_action": "scale out",
    }

    def stubborn(script):  # resubmits the same invalid diagnosis, like Nova did 102 times
        last = script.result("submit_diagnosis")
        if script.called("submit_diagnosis") and "already failed" in str(last):
            return "I could not submit the diagnosis."
        return Call("submit_diagnosis", bad)

    monkeypatch.setitem(offline.POLICIES, "diagnostics", stubborn)
    agent = factory.build_specialist("diagnostics", service.store, service.env)
    reply = str(agent(f"Incident {iid}: diagnose"))
    assert "could not submit" in reply
    kinds = [e["kind"] for e in service.store.list_events(iid) if e["message"] == "submit_diagnosis"]
    assert kinds.count("tool.completed") == 2 and kinds.count("tool.refused") == 1


def test_runbook_and_history_are_evidence_sources():
    from opsrelay.schemas import Evidence

    assert Evidence(source="runbook", value="RB-003").source == "runbook"
    assert Evidence(source="history", value="inc-0123456789 was the same").source == "history"


def test_remediation_takes_its_task_from_the_platform_not_the_coordinator(store, env):
    received = []

    def spy(role, inner):
        async def invoke(message):
            if role == "remediation":
                received.append(message)
            return await inner(message)

        return invoke

    invokers = {role: spy(role, factory.local_invoker(role, store, env)) for role in SPECIALISTS}
    service = IncidentService(store, env, invokers=invokers)
    incident = service.simulate("memory-leak")["incident"]
    [message] = received
    assert message == factory.REMEDIATION_TASK.format(incident_id=incident["id"])
    requests = [e["message"] for e in store.list_events(incident["id"]) if e["kind"] == "a2a.request"]
    assert any(r.startswith("-> remediation:") for r in requests)  # what the coordinator asked is still audited


def test_an_agent_that_answers_in_prose_is_asked_once_to_submit(service, monkeypatch):
    real = offline.POLICIES["diagnostics"]

    def prose_first(script):  # like Nova: writes the diagnosis as text instead of submitting it
        if "did not complete your task" not in script.prompt:
            return "Root cause: memory leak in the session cache. Confidence high."
        return real(script)

    monkeypatch.setitem(offline.POLICIES, "diagnostics", prose_first)
    incident = service.simulate("memory-leak")["incident"]
    assert incident["status"] == "awaiting_approval"
    kinds = [e["kind"] for e in service.store.list_events(incident["id"])]
    assert "contract.retry" in kinds and "contract.violation" not in kinds


def test_remediation_is_not_dispatched_without_a_diagnosis(service):
    from opsrelay.lifecycle import Status, transition

    incident = service.open_incident("something", run=False)["incident"]
    transition(service.store, incident["id"], Status.TRIAGING, actor="coordinator")
    transition(service.store, incident["id"], Status.INVESTIGATING, actor="triage")
    ask = next(
        t
        for t in factory.build_coordinator(service.store, service.env).tool_registry.registry.values()
        if t.tool_name == "ask_remediation"
    )
    import asyncio
    import json

    result = json.loads(asyncio.run(ask._tool_func(incident_id=incident["id"], request="restart it")))
    assert "needs the incident's diagnosis first" in result["error"]
