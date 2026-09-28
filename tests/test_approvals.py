"""The governance layer: proposal -> policy engine -> approval engine -> execution engine."""

import time

import pytest

from opsrelay import approvals, executor
from opsrelay.lifecycle import Status, transition
from opsrelay.schemas import RemediationProposal
from opsrelay.store import new_incident_id, now_iso
from opsrelay.store.base import new_record


def _investigating(store, confidence=0.95):
    iid = new_incident_id()
    store.put_incident(
        {
            "id": iid,
            "title": "t",
            "status": "investigating",
            "diagnosis": {"confidence": confidence},
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    )
    return iid


RUNBOOK_FOR = {
    "restart_service": "RB-002",
    "scale_service": "RB-003",
    "rollback_deployment": "RB-001",
    "flush_cache": "RB-005",
}


def _proposal(iid, action="restart_service", service="auth-service", risk="medium", **parameters):
    return RemediationProposal(
        incident_id=iid,
        action=action,
        service=service,
        parameters=parameters,
        risk=risk,
        rollback_plan="undo",
        rationale="because",
        runbook_id=RUNBOOK_FOR.get(action),
    )


def _propose(store, env, *args, **kwargs):
    return approvals.propose(store, env, _proposal(*args, **kwargs), agent="remediation")


def test_proposal_waits_for_a_person_and_touches_nothing(store, env):
    env.inject("memory-leak")
    iid = _investigating(store)
    approval = _propose(store, env, iid)
    assert (approval["status"], approval["policy"]["decision"], approval["risk"]) == (
        "pending",
        "APPROVAL_REQUIRED",
        "high",  # restart is medium, raised one level on the tier-1 auth-service
    )
    assert not env.metrics("auth-service")["healthy"]
    assert store.get_incident(iid)["status"] == "awaiting_approval"


def test_proposals_are_accepted_only_while_investigating(store, env):
    iid = _investigating(store)
    _propose(store, env, iid)
    with pytest.raises(approvals.ApprovalError, match="only while investigating"):
        _propose(store, env, iid)


@pytest.mark.parametrize(
    ("action", "message"),
    [("delete_database", "never allowed"), ("frobnicate", "not a known action")],
)
def test_denied_proposals_are_recorded_and_change_nothing(store, env, action, message):
    iid = _investigating(store)
    with pytest.raises(approvals.PolicyDenied, match=message):
        _propose(store, env, iid, action, "payments-db")
    [denied] = store.list_approvals(incident_id=iid)
    assert denied["status"] == "denied"
    assert store.get_incident(iid)["status"] == "investigating"


def test_low_confidence_diagnosis_cannot_be_acted_on(store, env):
    iid = _investigating(store, confidence=0.5)
    with pytest.raises(approvals.PolicyDenied, match="below 0.70"):
        _propose(store, env, iid, "scale_service", "inventory-service", "low", replicas=4)


def test_unknown_service_is_refused(store, env):
    iid = _investigating(store)
    with pytest.raises(approvals.ApprovalError, match="Unknown service"):
        _propose(store, env, iid, "restart_service", "no-such-svc")


def test_approval_executes_once_then_verifies(store, env):
    env.inject("memory-leak")
    iid = _investigating(store)
    approval = _propose(store, env, iid)

    decided = approvals.decide(store, env, approval["id"], approve=True, approver="alice")
    assert decided["status"] == "executed" and decided["result"]["ok"]
    assert env.metrics("auth-service")["healthy"]
    assert store.get_incident(iid)["status"] == "verifying"
    with pytest.raises(approvals.ApprovalError, match="already executed"):
        approvals.decide(store, env, approval["id"], approve=False, approver="bob")


def test_execution_is_idempotent(store, env):
    env.inject("bad-deploy")
    iid = _investigating(store)
    approval = _propose(store, env, iid, "rollback_deployment", "checkout-api", "high")
    approved = store.transition_approval(approval["id"], "pending", {"status": "approved"})

    first = executor.execute(store, env, approved)
    deploys = len(env.deployments("checkout-api"))
    second = executor.execute(store, env, approved)  # e.g. a retry after a lost reply

    assert first["ok"] and not first["replayed"]
    assert second["replayed"] and second["detail"] == first["detail"]
    assert len(env.deployments("checkout-api")) == deploys  # rolled back once, not twice
    assert "execution.deduplicated" in [e["kind"] for e in store.list_events(iid)]


def test_failed_execution_moves_the_incident_to_failed(store, env):
    iid = _investigating(store)
    # inventory-service has never been redeployed, so there is nothing to roll back to.
    approval = _propose(store, env, iid, "rollback_deployment", "inventory-service", "high")
    decided = approvals.decide(store, env, approval["id"], approve=True, approver="alice")
    assert decided["status"] == "failed"
    assert "no previous version" in decided["result"]["detail"]
    assert store.get_incident(iid)["status"] == "failed"
    assert "tool.failed" in [e["kind"] for e in store.list_events(iid)]


def test_decision_requires_an_approver(store, env):
    approval = _propose(store, env, _investigating(store))
    with pytest.raises(approvals.ApprovalError, match="approver"):
        approvals.decide(store, env, approval["id"], approve=True, approver=" ")


def _approved_but_not_run(store, env, monkeypatch, scenario="bad-deploy"):
    """An approval that was approved (incident remediating), as if the process died right after."""
    env.inject(scenario)
    iid = _investigating(store)
    pending = _propose(store, env, iid, "rollback_deployment", "checkout-api", "high")
    monkeypatch.setattr(approvals, "run_approved", lambda store, env, approval: approval)
    approval = approvals.decide(store, env, pending["id"], approve=True, approver="alice")
    monkeypatch.undo()
    assert (approval["status"], store.get_incident(iid)["status"]) == ("approved", "remediating")
    return iid, approval


def _stale_claim(store, approval, **extra):
    """The execution record left behind by an executor that stopped (its lease has expired)."""
    key = executor.idempotency_key(approval)
    store.put_record(new_record("execution", key, "running", owner="dead-host", attempt=1, lease_until=0, **extra))
    return key


def test_crash_before_the_action_ran_is_recovered_by_running_it_once(store, env, monkeypatch):
    iid, approval = _approved_but_not_run(store, env, monkeypatch)
    _stale_claim(store, approval)
    deploys = len(env.deployments("checkout-api"))

    assert approvals.recover(store, env) == [iid]

    assert len(env.deployments("checkout-api")) == deploys + 1  # rolled back exactly once
    assert store.get_approval(approval["id"])["status"] == "executed"
    assert store.get_incident(iid)["status"] == "verifying"
    recovering = next(e for e in store.list_events(iid) if e["kind"] == "execution.recovering")
    assert recovering["data"]["reconciled"] == "not_applied"


def test_crash_after_the_action_ran_is_reconciled_not_repeated(store, env, monkeypatch):
    iid, approval = _approved_but_not_run(store, env, monkeypatch)
    key = _stale_claim(store, approval)
    env.execute("rollback_deployment", "checkout-api", {}, idempotency_key=key)  # it ran, then the process died
    deploys = len(env.deployments("checkout-api"))

    approvals.recover(store, env)

    assert len(env.deployments("checkout-api")) == deploys  # not rolled back a second time
    done = store.get_approval(approval["id"])
    assert done["status"] == "executed" and "already been applied" in done["result"]["detail"]
    assert store.get_incident(iid)["status"] == "verifying"


def test_unknown_outcome_fails_safe_to_a_person(store, env, monkeypatch):
    iid, approval = _approved_but_not_run(store, env, monkeypatch)
    _stale_claim(store, approval)
    monkeypatch.setattr(env, "reconcile", lambda *args: "unknown")

    approvals.recover(store, env)

    assert store.get_approval(approval["id"])["status"] == "failed"
    incident = store.get_incident(iid)
    assert incident["status"] == "failed" and "a person must check" in incident["failure_reason"]


def test_a_live_lease_is_not_duplicated(store, env, monkeypatch):
    iid, approval = _approved_but_not_run(store, env, monkeypatch)
    key = executor.idempotency_key(approval)
    store.put_record(
        new_record("execution", key, "running", owner="busy-host", attempt=1, lease_until=time.time() + 60)
    )
    deploys = len(env.deployments("checkout-api"))

    result = executor.execute(store, env, approval)
    assert result["in_progress"] and approvals.recover(store, env) == []
    assert len(env.deployments("checkout-api")) == deploys
    assert store.get_incident(iid)["status"] == "remediating"  # left to the lease holder


def test_every_state_change_has_its_audit_event(store, env):
    """Atomic state + audit: each status the incident passed through has exactly one event."""
    env.inject("bad-deploy")
    iid = _investigating(store)
    approval = _propose(store, env, iid, "rollback_deployment", "checkout-api", "high")
    approvals.decide(store, env, approval["id"], approve=True, approver="alice")
    moves = [(e["data"]["from"], e["data"]["to"]) for e in store.list_events(iid) if e["kind"] == "status.changed"]
    assert moves == [
        ("investigating", "awaiting_approval"),
        ("awaiting_approval", "remediating"),
        ("remediating", "verifying"),
    ]
    # The decision event and the approval change were written together with the move.
    kinds = [e["kind"] for e in store.list_events(iid)]
    assert kinds.index("approval.approved") == kinds.index("status.changed", kinds.index("approval.requested")) + 1


def test_escalation_cancels_pending_approvals(store, env):
    iid = _investigating(store)
    approval = _propose(store, env, iid)
    transition(store, iid, Status.ESCALATED, actor="platform", reason="handing over")

    assert store.get_approval(approval["id"])["status"] == "cancelled"
    with pytest.raises(approvals.ApprovalError, match="already cancelled"):
        approvals.decide(store, env, approval["id"], approve=True, approver="alice")
