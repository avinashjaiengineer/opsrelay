"""The approval engine: between the policy engine and the execution engine.

    RemediationProposal -> policy engine -> DENY               refused; the agent is told why
                                         -> APPROVAL_REQUIRED  pending until a person decides
                                         -> ALLOW              approved by policy
    approved -> execution engine (once) -> VERIFYING, or FAILED if the action failed
    rejected -> ESCALATED

Agents never change infrastructure: they submit proposals. This is enforced here in code, so no
prompt can talk its way past it.
"""

from . import executor
from .environment import Environment
from .lifecycle import Status, transition
from .policy import Facts, get_policy
from .schemas import PolicyDecision, RemediationProposal
from .store import Record, Store, new_approval_id, now_iso


class ApprovalError(ValueError):
    pass


class PolicyDenied(ApprovalError):
    def __init__(self, decision: PolicyDecision):
        super().__init__("Denied by policy: " + "; ".join(decision.reasons))
        self.decision = decision


def _facts(store: Store, env: Environment, incident: Record, service: str) -> Facts:
    info = env.service_info(service)
    return Facts(
        service_tier=int(info.get("tier", 3)),
        service_max_replicas=int(info.get("max_replicas", 1)),
        deployed_versions=tuple(d["version"] for d in env.deployments(service)),
        diagnosis_confidence=(incident.get("diagnosis") or {}).get("confidence"),
        proposals_so_far=len(store.list_approvals(incident_id=incident["id"])),
    )


def propose(store: Store, env: Environment, proposal: RemediationProposal, *, agent: str) -> Record:
    incident = store.get_incident(proposal.incident_id)
    if incident is None:
        raise ApprovalError(f"Unknown incident {proposal.incident_id}")
    if incident["status"] != Status.INVESTIGATING:
        raise ApprovalError(
            f"Proposals are accepted only while investigating; {incident['id']} is {incident['status']}"
        )
    try:
        facts = _facts(store, env, incident, proposal.service)
    except KeyError as e:
        raise ApprovalError(str(e).strip("'\"")) from e

    decision = get_policy().evaluate(proposal, facts)
    store.record(
        incident["id"],
        agent,
        "remediation.proposed",
        f"Proposed {proposal.action} on {proposal.service} ({proposal.risk} risk): {proposal.rationale}",
        {"proposal": proposal.model_dump()},
        input=proposal.model_dump(),
    )
    store.record(
        incident["id"],
        "platform",
        "policy.evaluated",
        f"{decision.decision}, {decision.risk} risk: " + "; ".join(decision.reasons),
        {"decision": decision.model_dump()},
        output=decision.model_dump(),
    )
    approval = {
        "id": new_approval_id(),
        "incident_id": incident["id"],
        "agent": agent,
        "action": proposal.action,
        "service": proposal.service,
        "params": proposal.parameters,
        "rationale": proposal.rationale,
        "rollback_plan": proposal.rollback_plan,
        "agent_risk": proposal.risk,
        "risk": decision.risk,
        "policy": decision.model_dump(),
        "status": "denied" if decision.decision == "DENY" else "pending",
        "decided_by": f"policy:{decision.policy_version}" if decision.decision == "DENY" else None,
        "decided_at": now_iso() if decision.decision == "DENY" else None,
        "note": None,
        "result": None,
        "created_at": now_iso(),
    }
    store.put_approval(approval)
    if decision.decision == "DENY":
        raise PolicyDenied(decision)

    transition(store, incident["id"], Status.AWAITING_APPROVAL, actor=agent, reason=f"{proposal.action} proposed")
    store.record(
        incident["id"],
        "platform",
        "approval.requested",
        f"{proposal.action} on {proposal.service}: "
        + ("a person must approve" if decision.requires_human else "allowed by policy without approval"),
        {"approval_id": approval["id"], "risk": decision.risk},
    )
    if decision.decision == "ALLOW":
        return decide(
            store,
            env,
            approval["id"],
            approve=True,
            approver=f"policy:{decision.policy_version}",
            note="allowed by policy without human approval",
        )
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
    if not approver or not approver.strip():
        raise ApprovalError("approver is required")
    current = store.get_approval(approval_id)
    if current is None:
        raise ApprovalError(f"Unknown approval {approval_id}")
    incident = store.get_incident(current["incident_id"])
    if current["status"] == "pending" and incident and incident["status"] != Status.AWAITING_APPROVAL:
        raise ApprovalError(f"{incident['id']} is {incident['status']}, not awaiting approval")
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
        raise ApprovalError(f"Approval {approval_id} is already {store.get_approval(approval_id)['status']}")

    incident_id = approval["incident_id"]
    verb = "approved" if approve else "rejected"
    store.record(
        incident_id,
        approver,
        f"approval.{verb}",
        f"{approver} {verb} {approval['action']} on {approval['service']}" + (f": {note}" if note else ""),
        {"approval_id": approval_id, "decision": verb},
    )
    if not approve:
        reason = f"{approver} rejected {approval['action']}" + (f": {note}" if note else "")
        transition(store, incident_id, Status.ESCALATED, actor="platform", reason=reason, escalation_reason=reason)
        return approval

    transition(
        store, incident_id, Status.REMEDIATING, actor="platform", reason=f"{approval['action']} approved by {approver}"
    )
    result = executor.execute(store, env, approval)
    approval = store.transition_approval(
        approval_id,
        "approved",
        {"status": "executed" if result["ok"] else "failed", "result": result},
    ) or {**approval, "result": result}
    if result["ok"]:
        transition(store, incident_id, Status.VERIFYING, actor="platform", reason=result["detail"])
    else:
        transition(
            store,
            incident_id,
            Status.FAILED,
            actor="platform",
            reason=result["detail"],
            failure_reason=result["detail"],
        )
    return approval
