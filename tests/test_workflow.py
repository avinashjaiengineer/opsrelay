"""End-to-end incident response with offline agents and in-process specialists."""

import pytest

EXPECTED = {
    # scenario: service, severity, category, action, risk after policy
    "bad-deploy": ("checkout-api", "SEV1", "bad-deploy", "rollback_deployment", "high"),
    "memory-leak": ("auth-service", "SEV2", "memory-leak", "restart_service", "high"),
}


def _events(service, incident_id):
    return service.get_incident(incident_id)["events"]


def _kinds(service, incident_id):
    return [e["kind"] for e in _events(service, incident_id)]


def _path(service, incident_id):
    """The incident's status history, from its status.changed events."""
    moves = [e["data"] for e in _events(service, incident_id) if e["kind"] == "status.changed"]
    return ["open", *(m["to"] for m in moves)]


@pytest.mark.parametrize("scenario", sorted(EXPECTED))
def test_scenario_resolves_after_human_approval(service, scenario):
    svc_name, severity, category, action, risk = EXPECTED[scenario]

    opened = service.simulate(scenario)
    incident = opened["incident"]
    assert (incident["service"], incident["severity"], incident["category"]) == (svc_name, severity, category)
    assert incident["status"] == "awaiting_approval"
    assert "Waiting for human approval" in opened["report"]
    # Typed results are stored as validated by their contracts.
    assert incident["triage"]["confidence"] > 0.9
    assert incident["diagnosis"]["evidence"][0]["source"] == "metrics"

    # Nothing has been executed: the environment is still broken.
    assert not service.env.metrics(svc_name)["healthy"]
    [approval] = service.list_approvals()
    assert (approval["action"], approval["risk"], approval["status"]) == (action, risk, "pending")
    assert approval["policy"]["decision"] == "APPROVAL_REQUIRED"
    assert approval["rollback_plan"]

    decided = service.decide_approval(approval["id"], approve=True, approver="oncall@example.com")
    assert decided["approval"]["status"] == "executed"
    final = decided["incident"]
    assert final["status"] == "resolved"
    assert "## Root cause" in final["postmortem"] and "## Action items" in final["postmortem"]
    assert service.env.metrics(svc_name)["healthy"]

    assert _path(service, incident["id"]) == [
        "open",
        "triaging",
        "investigating",
        "awaiting_approval",
        "remediating",
        "verifying",
        "resolved",
    ]
    kinds = _kinds(service, incident["id"])
    for kind in (
        "incident.created",
        "incident.triaged",
        "diagnosis.completed",
        "remediation.proposed",
        "policy.evaluated",
        "approval.requested",
        "approval.approved",
        "tool.invoked",
        "tool.completed",
        "verification.completed",
        "status_update.internal",
        "status_update.customers",
        "incident.resolved",
    ):
        assert kind in kinds, kind
    assert "contract.violation" not in kinds
    # The human decision is attributed to the person who made it; agent events carry versions.
    events = _events(service, incident["id"])
    approved = next(e for e in events if e["kind"] == "approval.approved")
    assert (approved["actor"], approved["actor_type"]) == ("oncall@example.com", "human")
    triaged = next(e for e in events if e["kind"] == "incident.triaged")
    assert triaged["actor_type"] == "agent"
    assert {"agent_version", "model", "prompt_version"} <= set(triaged["data"])
    assert service.verify_audit(incident["id"])["ok"]


def test_rejected_action_escalates_and_changes_nothing(service):
    incident = service.simulate("bad-deploy")["incident"]
    [approval] = service.list_approvals()
    version_before = service.env.service_info("checkout-api")["version"]

    decided = service.decide_approval(approval["id"], approve=False, approver="lead@example.com", note="freeze")

    assert decided["approval"]["status"] == "rejected"
    assert decided["incident"]["status"] == "escalated"
    assert "freeze" in decided["incident"]["escalation_reason"]
    assert service.env.service_info("checkout-api")["version"] == version_before
    assert not any(e["actor"] == "platform" and e["kind"] == "tool.invoked" for e in _events(service, incident["id"]))
    assert _path(service, incident["id"])[-2:] == ["awaiting_approval", "escalated"]
    # Communications told engineering.
    assert "status_update.internal" in _kinds(service, incident["id"])


def test_low_risk_action_allowed_by_policy_runs_without_a_person(service):
    opened = service.simulate("traffic-spike")

    [approval] = service.list_approvals(status=None)
    assert approval["policy"]["decision"] == "ALLOW"
    assert approval["status"] == "executed"
    assert approval["decided_by"].startswith("policy:")
    assert service.env.metrics("inventory-service")["healthy"]
    # With no human in the loop, the agents verify and close the incident in the same run.
    assert service.get_incident(opened["incident"]["id"])["incident"]["status"] == "resolved"


def test_policy_denial_is_explained_and_the_incident_escalated(service, tmp_path, monkeypatch):
    from opsrelay.policy import get_policy

    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 9\nactions:\n  rollback_deployment: {risk: high, allowed: false}\n", encoding="utf-8")
    monkeypatch.setenv("OPSRELAY_POLICY_FILE", str(policy))
    from opsrelay.config import get_settings

    get_settings.cache_clear()
    get_policy.cache_clear()

    opened = service.simulate("bad-deploy")

    [denied] = service.list_approvals(status=None)
    assert denied["status"] == "denied"
    assert denied["policy"]["decision"] == "DENY"
    kinds = _kinds(service, opened["incident"]["id"])
    assert "remediation.denied" in kinds and "remediation.declined" in kinds
    assert opened["incident"]["status"] == "escalated"
    assert not any(
        e["kind"] == "tool.invoked" and e["actor"] == "platform" for e in _events(service, opened["incident"]["id"])
    )


def test_incident_without_detectable_cause_is_escalated(service):
    result = service.open_incident("Users say the site feels slow", "No alert fired.")
    incident = result["incident"]
    # Everything is healthy: triage reports it is inconclusive and nothing is proposed.
    assert service.list_approvals() == []
    assert incident["status"] == "escalated"
    assert "triage.inconclusive" in _kinds(service, incident["id"])
    assert _path(service, incident["id"]) == ["open", "triaging", "escalated"]


def test_duplicate_alerts_are_deduplicated(service):
    first = service.open_incident("disk full", "node-7", external_ref="alert-123", run=False)
    second = service.open_incident("disk full", "node-7", external_ref="alert-123", run=False)
    assert second["deduplicated"] is True
    assert second["incident"]["id"] == first["incident"]["id"]
    assert len(service.list_incidents()) == 1


def test_a_decided_approval_cannot_be_decided_again(service):
    from opsrelay.approvals import ApprovalError

    service.simulate("bad-deploy")
    [approval] = service.list_approvals()
    service.decide_approval(approval["id"], approve=True, approver="a@example.com", run=False)
    with pytest.raises(ApprovalError, match="already executed"):
        service.decide_approval(approval["id"], approve=False, approver="b@example.com", run=False)
