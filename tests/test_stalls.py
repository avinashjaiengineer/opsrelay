"""A coordinator that stops mid-incident gets one nudge, then the incident goes to a person."""

from opsrelay import service as service_module
from opsrelay.lifecycle import Status, transition
from opsrelay.policy import Facts, load_policy
from opsrelay.schemas import RemediationProposal


class LazyCoordinator:
    """Answers without doing anything, like a model that ends its turn too early."""

    def __init__(self):
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return "Looks fine to me."


def _investigating(service):
    incident = service.open_incident("web-frontend latency alert flapping", run=False)["incident"]
    transition(service.store, incident["id"], Status.TRIAGING, actor="coordinator")
    transition(service.store, incident["id"], Status.INVESTIGATING, actor="triage", severity="SEV4")
    return incident["id"]


def test_a_stalled_incident_is_nudged_then_handed_to_a_person(service, monkeypatch):
    lazy = LazyCoordinator()
    monkeypatch.setattr(service_module, "build_coordinator", lambda *_a: lazy)
    iid = _investigating(service)
    service.run_coordinator(iid, "New incident")
    assert len(lazy.prompts) == 2 and "is not finished" in lazy.prompts[1]
    incident = service.store.get_incident(iid)
    assert incident["status"] == "escalated"
    assert "nothing waiting on a person: handed to a person" in incident["escalation_reason"]
    kinds = [e["kind"] for e in service.store.list_events(iid)]
    assert kinds.count("coordinator.stalled") == 1


def test_a_nudge_that_works_needs_no_person(service, monkeypatch):
    real = service_module.build_coordinator
    calls = []

    def first_lazy_then_real(*args):
        calls.append(1)
        return LazyCoordinator() if len(calls) == 1 else real(*args)

    monkeypatch.setattr(service_module, "build_coordinator", first_lazy_then_real)
    service.env.inject("memory-leak")
    incident = service.open_incident("auth-service pods OOMKilled", run=False)["incident"]
    service.run_coordinator(incident["id"], "New incident")
    assert len(calls) == 2
    assert service.store.get_incident(incident["id"])["status"] == "awaiting_approval"


def test_waiting_on_a_person_is_not_a_stall(service):
    incident = service.simulate("bad-deploy")["incident"]
    assert incident["status"] == "awaiting_approval"
    assert service.stalled(incident["id"]) is None
    assert "coordinator.stalled" not in {e["kind"] for e in service.store.list_events(incident["id"])}


def test_policy_checks_the_runbook_covers_the_service():
    proposal = RemediationProposal(
        incident_id="inc-0123456789",
        action="flush_cache",
        service="inventory-service",
        risk="low",
        rollback_plan="n/a",
        rationale="why",
        runbook_id="RB-005",
    )
    facts = Facts(
        service_tier=2,
        service_max_replicas=8,
        deployed_versions=("1",),
        diagnosis_confidence=0.95,
        proposals_so_far=0,
        runbook_actions=("flush_cache", "restart_service"),
        runbook_services=("redis-cache",),
    )
    decision = load_policy().evaluate(proposal, facts)
    assert decision.decision == "APPROVAL_REQUIRED"
    assert "RB-005 is written for redis-cache, not inventory-service" in decision.reasons[-1]
