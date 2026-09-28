"""Human approval gates.

Agents never change infrastructure directly. The remediation agent *proposes* an action; the
proposal is stored as a pending approval and nothing happens until a person approves it (or the
action's risk is at or below OPSRELAY_AUTO_APPROVE_RISK). Only then does the platform, not the
agent, execute it. This is enforced here in code, so no prompt can talk its way past it.
"""

from .config import get_settings
from .environment import ACTIONS, RISK_ORDER, Environment
from .store import Record, Store, new_approval_id, now_iso


class ApprovalError(ValueError):
    pass


def _auto_approved(risk: str) -> bool:
    threshold = get_settings().auto_approve_risk
    return threshold != "none" and RISK_ORDER.index(risk) <= RISK_ORDER.index(threshold)


def propose(
    store: Store,
    env: Environment,
    *,
    incident_id: str,
    agent: str,
    action: str,
    service: str,
    params: Record | None,
    rationale: str,
) -> Record:
    if store.get_incident(incident_id) is None:
        raise ApprovalError(f"Unknown incident {incident_id}")
    if action not in ACTIONS:
        raise ApprovalError(f"Unknown action '{action}'. Allowed: {', '.join(ACTIONS)}")
    env.service_info(service)  # raises KeyError for an unknown service
    for existing in store.list_approvals(status="pending", incident_id=incident_id):
        if existing["action"] == action and existing["service"] == service:
            return existing  # idempotent: the agent asked twice

    risk = env.action_risk(action, service)
    approval = {
        "id": new_approval_id(),
        "incident_id": incident_id,
        "agent": agent,
        "action": action,
        "service": service,
        "params": params or {},
        "rationale": rationale,
        "risk": risk,
        "status": "pending",
        "decided_by": None,
        "decided_at": None,
        "note": None,
        "result": None,
        "created_at": now_iso(),
    }
    store.put_approval(approval)
    store.record(
        incident_id,
        agent,
        "approval.requested",
        f"Proposed {action} on {service} (risk: {risk}): {rationale}",
        {"approval_id": approval["id"]},
    )
    if _auto_approved(risk):
        return decide(store, env, approval["id"], approve=True, approver="policy:auto-approve", note=f"risk {risk}")
    store.update_incident(incident_id, status="awaiting_approval")
    return approval


def decide(
    store: Store,
    env: Environment,
    approval_id: str,
    *,
    approve: bool,
    approver: str,
    note: str | None = None,
) -> Record:
    if not approver:
        raise ApprovalError("approver is required")
    approval = store.transition_approval(
        approval_id,
        "pending",
        {
            "status": "approved" if approve else "rejected",
            "decided_by": approver,
            "decided_at": now_iso(),
            "note": note,
        },
    )
    if approval is None:
        current = store.get_approval(approval_id)
        if current is None:
            raise ApprovalError(f"Unknown approval {approval_id}")
        raise ApprovalError(f"Approval {approval_id} is already {current['status']}")

    incident_id = approval["incident_id"]
    verb = "approved" if approve else "rejected"
    store.record(
        incident_id,
        approver,
        f"approval.{verb}",
        f"{approver} {verb} {approval['action']} on {approval['service']}" + (f": {note}" if note else ""),
        {"approval_id": approval_id},
    )
    if not approve:
        store.update_incident(incident_id, status="investigating")
        return approval

    try:
        result = env.execute(approval["action"], approval["service"], approval["params"])
    except Exception as e:  # noqa: BLE001 - any connector failure is recorded, not raised
        result = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    approval = store.transition_approval(
        approval_id, "approved", {"status": "executed" if result["ok"] else "failed", "result": result}
    ) or {**approval, "result": result}
    store.record(
        incident_id,
        "platform",
        "action.executed" if result["ok"] else "action.failed",
        result["detail"],
        {"approval_id": approval_id},
    )
    store.update_incident(incident_id, status="investigating")
    return approval
