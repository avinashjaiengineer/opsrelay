import pytest

from opsrelay import approvals
from opsrelay.store import new_incident_id, now_iso


@pytest.fixture
def incident_id(store):
    iid = new_incident_id()
    store.put_incident({"id": iid, "title": "t", "status": "open", "created_at": now_iso(), "updated_at": now_iso()})
    return iid


def _propose(store, env, incident_id, action="restart_service", service="auth-service", **params):
    return approvals.propose(
        store,
        env,
        incident_id=incident_id,
        agent="remediation",
        action=action,
        service=service,
        params=params,
        rationale="because",
    )


def test_proposal_does_not_touch_the_environment(store, env, incident_id):
    env.inject("memory-leak")
    approval = _propose(store, env, incident_id)
    assert approval["status"] == "pending"
    assert not env.metrics("auth-service")["healthy"]
    assert store.get_incident(incident_id)["status"] == "awaiting_approval"


def test_risk_is_raised_for_tier_one_services(store, env, incident_id):
    assert _propose(store, env, incident_id, "scale_service", "inventory-service", replicas=4)["risk"] == "low"
    assert _propose(store, env, incident_id, "scale_service", "checkout-api", replicas=4)["risk"] == "medium"
    assert _propose(store, env, incident_id, "rollback_deployment", "checkout-api")["risk"] == "high"


def test_repeat_proposal_is_idempotent(store, env, incident_id):
    first = _propose(store, env, incident_id)
    second = _propose(store, env, incident_id)
    assert first["id"] == second["id"]
    assert len(store.list_approvals(incident_id=incident_id)) == 1


@pytest.mark.parametrize(
    ("action", "service", "message"),
    [("delete_database", "payments-db", "Unknown action"), ("restart_service", "no-such-svc", "Unknown service")],
)
def test_invalid_proposals_are_refused(store, env, incident_id, action, service, message):
    with pytest.raises((approvals.ApprovalError, KeyError), match=message):
        _propose(store, env, incident_id, action, service)
    assert store.list_approvals() == []


def test_approval_executes_once(store, env, incident_id):
    env.inject("memory-leak")
    approval = _propose(store, env, incident_id)

    decided = approvals.decide(store, env, approval["id"], approve=True, approver="alice")
    assert decided["status"] == "executed"
    assert decided["result"]["ok"] is True
    assert env.metrics("auth-service")["healthy"]

    with pytest.raises(approvals.ApprovalError, match="already executed"):
        approvals.decide(store, env, approval["id"], approve=False, approver="bob")


def test_failed_execution_is_recorded(store, env, incident_id):
    approval = _propose(store, env, incident_id, "rollback_deployment", "inventory-service")  # nothing to roll back
    decided = approvals.decide(store, env, approval["id"], approve=True, approver="alice")
    assert decided["status"] == "failed"
    assert "no previous version" in decided["result"]["detail"]
    assert store.list_events(incident_id)[-1]["kind"] == "action.failed"


def test_decision_requires_an_approver(store, env, incident_id):
    approval = _propose(store, env, incident_id)
    with pytest.raises(approvals.ApprovalError, match="approver"):
        approvals.decide(store, env, approval["id"], approve=True, approver="")


def test_scale_is_bounded(store, env, incident_id):
    approval = _propose(store, env, incident_id, "scale_service", "inventory-service", replicas=500)
    decided = approvals.decide(store, env, approval["id"], approve=True, approver="alice")
    assert decided["status"] == "failed"
    assert env.service_info("inventory-service")["replicas"] == 2
