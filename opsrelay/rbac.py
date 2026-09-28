"""What each role may do: authorization for every coordinator API action.

    Role                What it can do
    viewer              see incidents, approvals, health, contracts and the policy
    operator            + open and simulate incidents, test the policy, approve low-risk actions
    sre                 + approve medium- and high-risk actions (not on SEV1 incidents)
    incident_commander  + approve anything, including SEV1 incidents and critical-risk actions
    admin               propose, review and activate policy versions; read the audit
    auditor             read everything, including the audit chain and dead letters; change nothing

Approving is separated from configuring: an admin can change the policy but not approve actions,
and an incident commander can approve actions but not change the policy.
"""

from .auth import Principal

READ = frozenset({"viewer", "operator", "sre", "incident_commander", "admin", "auditor"})
AUDIT = frozenset({"auditor", "admin", "sre", "incident_commander"})
OPERATE = frozenset({"operator", "sre", "incident_commander"})
TEST_POLICY = frozenset({"operator", "sre", "incident_commander", "admin"})
POLICY_ADMIN = frozenset({"admin"})

ACTIONS: dict[str, frozenset[str]] = {
    "whoami": READ,
    "get_incident": READ,
    "list_incidents": READ,
    "list_approvals": READ,
    "health": READ,
    "get_contracts": READ,
    "get_policy": READ,
    "verify_audit": AUDIT,
    "list_dead_letters": AUDIT,
    "list_policies": AUDIT,
    "open_incident": OPERATE,
    "simulate": OPERATE,
    "test_policy": TEST_POLICY,
    "propose_policy": POLICY_ADMIN,
    "review_policy": POLICY_ADMIN,
    "activate_policy": POLICY_ADMIN,
    "decide_approval": OPERATE,  # plus the per-approval rule below
}


class Forbidden(Exception):
    pass


def authorize(principal: Principal, action: str) -> None:
    allowed = ACTIONS.get(action)
    if allowed is None:
        return  # unknown actions are rejected by the handler itself
    if not principal.roles & allowed:
        raise Forbidden(f"{principal.name} ({', '.join(sorted(principal.roles)) or 'no roles'}) may not {action}")


def roles_to_decide(risk: str, severity: str | None) -> frozenset[str]:
    """Who may approve or reject a remediation with this risk, on an incident with this severity."""
    if risk == "critical" or severity == "SEV1":
        return frozenset({"incident_commander"})
    if risk in ("medium", "high"):
        return frozenset({"sre", "incident_commander"})
    return frozenset({"operator", "sre", "incident_commander"})


def authorize_decision(principal: Principal, risk: str, severity: str | None) -> str:
    """The role the principal decides as, or Forbidden."""
    needed = roles_to_decide(risk, severity)
    held = principal.roles & needed
    if not held:
        raise Forbidden(
            f"{principal.name} may not decide a {risk}-risk action on a {severity or 'unrated'} incident; "
            f"that needs one of: {', '.join(sorted(needed))}"
        )
    order = ["incident_commander", "sre", "operator"]
    return next(r for r in order if r in held)
