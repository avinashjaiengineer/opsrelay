"""The approval engine: between the policy engine and the execution engine.

    RemediationProposal -> policy engine -> DENY               refused; the agent is told why
                                         -> APPROVAL_REQUIRED  pending until a person decides
                                         -> ALLOW              approved by policy
    approved -> execution engine (once) -> VERIFYING, or FAILED if the action failed
    rejected -> ESCALATED

Agents never change infrastructure: they submit proposals. Every step is one atomic commit of the
approval, the incident's status and the audit events describing them. If the process stops while
an action runs, `recover` finishes the job: the execution engine reconciles with the environment
instead of guessing.
"""

from . import executor, runbooks, telemetry
from .environment import Environment
from .lifecycle import IllegalTransition, Status, transition
from .policy import Facts, get_policy
from .schemas import PolicyDecision, RemediationProposal
from .store import Record, Store, new_approval_id, now_iso
from .store.base import ApprovalMove, Commit, make_event


class ApprovalError(ValueError):
    pass


class PolicyDenied(ApprovalError):
    def __init__(self, decision: PolicyDecision):
        super().__init__("Denied by policy: " + "; ".join(decision.reasons))
        self.decision = decision


def _facts(store: Store, env: Environment, incident: Record, service: str, runbook_id: str | None = None) -> Facts:
    info = env.service_info(service)
    runbook = runbooks.get(runbook_id) if runbook_id else None
    return Facts(
        runbook_actions=runbook.actions if runbook else None,
        service_tier=int(info.get("tier", 3)),
        service_max_replicas=int(info.get("max_replicas", 1)),
        deployed_versions=tuple(d["version"] for d in env.deployments(service)),
        diagnosis_confidence=(incident.get("diagnosis") or {}).get("confidence"),
        proposals_so_far=len(store.list_approvals(incident_id=incident["id"])),
    )


def propose(
    store: Store, env: Environment, proposal: RemediationProposal, *, agent: str, meta: Record | None = None
) -> Record:
    incident = store.get_incident(proposal.incident_id)
    if incident is None:
        raise ApprovalError(f"Unknown incident {proposal.incident_id}")
    if incident["status"] != Status.INVESTIGATING:
        raise ApprovalError(
            f"Proposals are accepted only while investigating; {incident['id']} is {incident['status']}"
        )
    try:
        facts = _facts(store, env, incident, proposal.service, proposal.runbook_id)
    except KeyError as e:
        raise ApprovalError(str(e).strip("'\"")) from e

    decision = get_policy(store).evaluate(proposal, facts)
    telemetry.count("policy_decisions_total", decision=decision.decision)
    denied = decision.decision == "DENY"
    approval = {
        "id": new_approval_id(),
        "incident_id": incident["id"],
        "agent": agent,
        "action": proposal.action,
        "service": proposal.service,
        "params": proposal.parameters,
        "rationale": proposal.rationale,
        "rollback_plan": proposal.rollback_plan,
        "runbook_id": proposal.runbook_id,
        "agent_risk": proposal.risk,
        "risk": decision.risk,
        "severity": incident.get("severity"),
        "policy": decision.model_dump(),
        "status": "denied" if denied else "pending",
        "decided_by": f"policy:{decision.policy_version}" if denied else None,
        "decided_at": now_iso() if denied else None,
        "note": None,
        "result": None,
        "created_at": now_iso(),
    }
    events = [
        make_event(
            incident["id"],
            agent,
            "remediation.proposed",
            f"Proposed {proposal.action} on {proposal.service} ({proposal.risk} risk): {proposal.rationale}",
            {"proposal": proposal.model_dump(), "approval_id": approval["id"], **(meta or {})},
            input=proposal.model_dump(),
        ),
        make_event(
            incident["id"],
            "platform",
            "policy.evaluated",
            f"{decision.decision}, {decision.risk} risk: " + "; ".join(decision.reasons),
            {"decision": decision.model_dump(), "approval_id": approval["id"]},
            output=decision.model_dump(),
        ),
    ]
    if denied:
        events.append(
            make_event(
                incident["id"],
                agent,
                "remediation.denied",
                f"Policy denied {proposal.action}: " + "; ".join(decision.reasons),
                {"decision": decision.model_dump(), **(meta or {})},
            )
        )
        if store.commit(Commit(new_approvals=[approval], events=events)) is None:
            raise ApprovalError(f"Could not record the denied proposal for {incident['id']}")
        raise PolicyDenied(decision)

    events.append(
        make_event(
            incident["id"],
            "platform",
            "approval.requested",
            f"{proposal.action} on {proposal.service}: "
            + ("a person must approve" if decision.requires_human else "allowed by policy without approval"),
            {"approval_id": approval["id"], "risk": decision.risk},
        )
    )
    transition(
        store,
        incident["id"],
        Status.AWAITING_APPROVAL,
        actor=agent,
        reason=f"{proposal.action} proposed",
        new_approvals=[approval],
        events=events,
    )
    if decision.decision == "ALLOW":
        return decide(
            store,
            env,
            approval["id"],
            approve=True,
            approver=f"policy:{decision.policy_version}",
            note="allowed by policy without human approval",
            role="policy",
            verified=True,
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
    role: str | None = None,
    verified: bool = False,
) -> Record:
    """Record a person's (or the policy's) decision and act on it. `role` and `verified` say who the
    approver is according to the authentication layer (see opsrelay.auth)."""
    if not approver or not approver.strip():
        raise ApprovalError("approver is required")
    current = store.get_approval(approval_id)
    if current is None:
        raise ApprovalError(f"Unknown approval {approval_id}")
    if current["status"] != "pending":
        raise ApprovalError(f"Approval {approval_id} is already {current['status']}")
    incident_id = current["incident_id"]
    incident = store.get_incident(incident_id)
    if incident is None or incident["status"] != Status.AWAITING_APPROVAL:
        raise ApprovalError(f"{incident_id} is {incident and incident['status']}, not awaiting approval")

    verb = "approved" if approve else "rejected"
    decision = {
        "status": verb,
        "decided_by": approver,
        "decided_by_role": role,
        "identity_verified": verified,
        "decided_at": now_iso(),
        "note": note,
    }
    decision_event = make_event(
        incident_id,
        approver,
        f"approval.{verb}",
        f"{approver} {verb} {current['action']} on {current['service']}" + (f": {note}" if note else ""),
        {"approval_id": approval_id, "decision": verb, "role": role, "identity_verified": verified},
    )
    move = ApprovalMove(approval_id, "pending", decision)
    try:
        if approve:
            transition(
                store,
                incident_id,
                Status.REMEDIATING,
                actor="platform",
                reason=f"{current['action']} approved by {approver}",
                approval_moves=[move],
                events=[decision_event],
            )
        else:
            reason = f"{approver} rejected {current['action']}" + (f": {note}" if note else "")
            transition(
                store,
                incident_id,
                Status.ESCALATED,
                actor="platform",
                reason=reason,
                escalation_reason=reason,
                approval_moves=[move],
                events=[decision_event],
            )
    except IllegalTransition as e:
        now = store.get_approval(approval_id) or current
        if now["status"] != "pending":
            raise ApprovalError(f"Approval {approval_id} is already {now['status']}") from e
        raise ApprovalError(str(e)) from e
    approval = store.get_approval(approval_id)
    return run_approved(store, env, approval) if approve else approval


def run_approved(store: Store, env: Environment, approval: Record) -> Record:
    """Execute an approved action (once) and move the incident on. Safe to call again for the same
    approval, e.g. after a restart: the execution engine replays or reconciles instead of repeating."""
    result = executor.execute(store, env, approval)
    if result.get("in_progress"):
        return approval  # another executor holds the lease; it (or recovery) finishes the job
    outcome = {"status": "executed" if result["ok"] else "failed", "result": result}
    move = ApprovalMove(approval["id"], "approved", outcome)
    try:
        if result["ok"]:
            transition(
                store,
                approval["incident_id"],
                Status.VERIFYING,
                actor="platform",
                reason=result["detail"],
                approval_moves=[move],
            )
        else:
            transition(
                store,
                approval["incident_id"],
                Status.FAILED,
                actor="platform",
                reason=result["detail"],
                failure_reason=result["detail"],
                approval_moves=[move],
            )
    except IllegalTransition:
        pass  # someone else (e.g. a recovery pass) already moved it on
    return store.get_approval(approval["id"]) or approval


def recover(store: Store, env: Environment) -> list[str]:
    """Finish remediations interrupted by a crash or restart. Returns the incidents touched."""
    touched = []
    for incident in store.list_incidents(limit=200):
        if incident["status"] != Status.REMEDIATING:
            continue
        for approval in store.list_approvals(status="approved", incident_id=incident["id"]):
            done = run_approved(store, env, approval)
            if done["status"] != "approved":
                touched.append(incident["id"])
    return touched
