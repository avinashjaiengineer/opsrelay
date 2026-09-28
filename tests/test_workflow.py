"""End-to-end incident response with offline agents and in-process specialists."""

import pytest

from opsrelay.config import get_settings

EXPECTED = {
    "bad-deploy": ("checkout-api", "sev1", "bad-deploy", "rollback_deployment", "high"),
    "memory-leak": ("auth-service", "sev2", "memory-leak", "restart_service", "high"),
    "traffic-spike": ("inventory-service", "sev3", "saturation", "scale_service", "low"),
}


def _kinds(service, incident_id):
    return [e["kind"] for e in service.get_incident(incident_id)["events"]]


@pytest.mark.parametrize("scenario", sorted(EXPECTED))
def test_scenario_resolves_after_human_approval(service, scenario):
    svc_name, severity, category, action, risk = EXPECTED[scenario]

    opened = service.simulate(scenario)
    incident = opened["incident"]
    assert incident["service"] == svc_name
    assert incident["severity"] == severity
    assert incident["category"] == category
    assert incident["status"] == "awaiting_approval"
    assert "Waiting for human approval" in opened["report"]

    # Nothing has been executed: the environment is still broken.
    assert not service.env.metrics(svc_name)["healthy"]
    [approval] = service.list_approvals()
    assert (approval["action"], approval["risk"], approval["status"]) == (action, risk, "pending")

    decided = service.decide_approval(approval["id"], approve=True, approver="oncall@example.com")
    assert decided["approval"]["status"] == "executed"
    final = decided["incident"]
    assert final["status"] == "resolved"
    assert "## Root cause" in final["postmortem"]
    assert service.env.metrics(svc_name)["healthy"]

    kinds = _kinds(service, incident["id"])
    for kind in (
        "incident.opened",
        "a2a.request",
        "a2a.response",
        "triage",
        "diagnosis",
        "approval.requested",
        "approval.approved",
        "action.executed",
        "mitigated",
        "status_update.internal",
        "status_update.customers",
        "resolved",
    ):
        assert kind in kinds, kind
    # The human decision is attributed to the person who made it.
    approved = next(e for e in service.get_incident(incident["id"])["events"] if e["kind"] == "approval.approved")
    assert approved["actor"] == "oncall@example.com"


def test_rejected_action_escalates_and_changes_nothing(service):
    incident = service.simulate("bad-deploy")["incident"]
    [approval] = service.list_approvals()
    version_before = service.env.service_info("checkout-api")["version"]

    decided = service.decide_approval(approval["id"], approve=False, approver="lead@example.com", note="freeze")

    assert decided["approval"]["status"] == "rejected"
    assert decided["incident"]["status"] == "escalated"
    assert "freeze" in decided["incident"]["escalation_reason"]
    assert service.env.service_info("checkout-api")["version"] == version_before
    assert "action.executed" not in _kinds(service, incident["id"])


def test_low_risk_action_can_be_auto_approved_by_policy(service, monkeypatch):
    monkeypatch.setenv("OPSRELAY_AUTO_APPROVE_RISK", "low")
    get_settings.cache_clear()

    opened = service.simulate("traffic-spike")

    [approval] = service.list_approvals(status=None)
    assert approval["status"] == "executed"
    assert approval["decided_by"] == "policy:auto-approve"
    assert service.env.metrics("inventory-service")["healthy"]
    # With no human in the loop, the agents verify and close the incident in the same run.
    assert service.get_incident(opened["incident"]["id"])["incident"]["status"] == "resolved"


def test_high_risk_action_is_not_auto_approved_by_low_policy(service, monkeypatch):
    monkeypatch.setenv("OPSRELAY_AUTO_APPROVE_RISK", "low")
    get_settings.cache_clear()

    service.simulate("bad-deploy")

    [approval] = service.list_approvals()
    assert approval["status"] == "pending"  # rollback on a tier-1 service is high risk


def test_incident_without_detectable_cause_is_escalated(service):
    result = service.open_incident("Users say the site feels slow", "No alert fired.")
    incident = result["incident"]
    # Everything is healthy, so triage cannot identify a service and nothing is proposed.
    assert service.list_approvals() == []
    assert incident["status"] == "escalated"


def test_duplicate_alerts_are_deduplicated(service):
    first = service.open_incident("disk full", "node-7", external_ref="alert-123", run=False)
    second = service.open_incident("disk full", "node-7", external_ref="alert-123", run=False)
    assert second["deduplicated"] is True
    assert second["incident"]["id"] == first["incident"]["id"]
    assert len(service.list_incidents()) == 1
