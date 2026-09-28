"""Agent contracts are enforced by the platform at every delegation, whatever the agent does."""

import asyncio
import json

import pytest

from opsrelay import resilience
from opsrelay.agents import factory
from opsrelay.agents.tools import diagnostics_tools, triage_tools
from opsrelay.config import SPECIALISTS
from opsrelay.resilience import CircuitBreaker, CircuitOpen
from opsrelay.service import IncidentService


def _ask(service, role, invoke, incident_id, request="do your job"):
    tool = factory._delegation_tool(role, invoke, service.store)
    return json.loads(asyncio.run(tool._tool_func(incident_id=incident_id, request=request)))


def _open(service):
    return service.open_incident("checkout-api errors", "5xx above 20%", run=False)["incident"]["id"]


def _kinds(service, iid):
    return [e["kind"] for e in service.get_incident(iid)["events"]]


async def _silent(message):
    return "Done! Everything looks fine."  # replies without submitting anything


def test_dispatch_outside_the_agents_states_is_refused(service):
    iid = _open(service)
    reply = _ask(service, "diagnostics", _silent, iid)  # the incident hasn't been triaged
    assert "acts only on incidents that are investigating" in reply["error"]
    assert "contract.violation" in _kinds(service, iid)
    assert "a2a.request" not in _kinds(service, iid)


def test_returning_without_a_typed_result_is_a_violation(service):
    iid = _open(service)
    reply = _ask(service, "triage", _silent, iid)
    assert "without submitting a TriageResult" in reply["contract_violation"]
    assert reply["incident_status"] == "triaging"
    violation = next(e for e in service.get_incident(iid)["events"] if e["kind"] == "contract.violation")
    assert violation["data"] == {"agent": "triage", "phase": "result"}


def test_a_compliant_agent_returns_its_typed_result(service):
    service.env.inject("bad-deploy")
    iid = _open(service)
    reply = _ask(service, "triage", factory.local_invoker("triage", service.store, service.env), iid)
    assert reply["incident_status"] == "investigating"
    assert reply["result"]["kind"] == "incident.triaged"
    assert (reply["result"]["severity"], reply["result"]["service"]) == ("SEV1", "checkout-api")


def _tool(tools, name):
    return next(t for t in tools if t.tool_name == name)


def test_invalid_output_is_rejected_by_pydantic(service):
    service.env.inject("bad-deploy")
    iid = _open(service)
    _ask(service, "triage", factory.local_invoker("triage", service.store, service.env), iid)
    submit = _tool(diagnostics_tools(service.store, service.env), "submit_diagnosis")

    result = json.loads(
        submit._tool_func(
            incident_id=iid,
            root_cause="it broke",
            category="bad-deploy",
            evidence=[{"source": "gut feeling", "value": "trust me"}],
            confidence=1.7,
            affected_component="x",
            recommended_action="y",
        )
    )
    assert "DiagnosisResult is invalid" in result["error"]
    problems = " ".join(result["problems"])
    assert "confidence" in problems and "evidence.0.source" in problems
    assert "diagnosis" not in service.store.get_incident(iid)


def test_an_agent_cannot_act_outside_its_states_through_its_tools(service):
    iid = _open(service)  # still open: triage hasn't been dispatched
    submit = _tool(triage_tools(service.store, service.env), "submit_triage")
    result = json.loads(
        submit._tool_func(
            incident_id=iid,
            severity="SEV1",
            service="checkout-api",
            customer_impact="c",
            rationale="r",
            confidence=0.9,
        )
    )
    assert "acts only on incidents that are triaging" in result["error"]
    assert service.store.get_incident(iid)["status"] == "open"


def test_unavailable_agent_is_retried_then_dead_lettered(service):
    calls = []

    async def down(message):
        calls.append(message)
        raise ConnectionError("connection refused")

    service.env.inject("bad-deploy")
    invokers = {role: factory.local_invoker(role, service.store, service.env) for role in SPECIALISTS}
    invokers["diagnostics"] = down
    svc = IncidentService(store=service.store, env=service.env, invokers=invokers)

    opened = svc.open_incident("checkout-api 5xx", "errors")
    iid = opened["incident"]["id"]

    assert len(calls) == 3  # agent_max_attempts
    assert _kinds(svc, iid).count("agent.retry") == 3
    [letter] = svc.dead_letters()
    assert letter["agent"] == "diagnostics" and len(letter["attempts"]) == 3
    assert opened["incident"]["status"] == "escalated"
    assert svc.verify_audit(iid)["ok"]


def test_a_lost_reply_is_not_repeated(service):
    """If the agent did its job but the reply was lost, the retry sees the result and stops."""
    service.env.inject("bad-deploy")
    real = factory.local_invoker("triage", service.store, service.env)
    calls = []

    async def flaky(message):
        calls.append(message)
        await real(message)
        raise TimeoutError("reply lost")

    iid = _open(service)
    reply = _ask(service, "triage", flaky, iid)
    assert len(calls) == 1
    assert reply["incident_status"] == "investigating"
    assert _kinds(service, iid).count("incident.triaged") == 1


def test_circuit_breaker_opens_and_recovers(monkeypatch):
    clock = [1000.0]  # a controlled clock: no real sleeps, no timer-resolution flakiness
    monkeypatch.setattr(resilience.time, "monotonic", lambda: clock[0])
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=30)
    breaker.record_failure()
    breaker.before_call()  # one failure: still closed
    breaker.record_failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpen):
        breaker.before_call()

    clock[0] += 31
    assert breaker.state == "half-open"
    breaker.before_call()
    breaker.record_failure()  # the trial call failed: open again
    assert breaker.state == "open"
    clock[0] += 31
    breaker.record_success()
    assert breaker.state == "closed"
