"""A strict contract for every agent, enforced by the platform rather than by prompts.

A contract states:

- acts_in: the incident states in which the agent may be dispatched (checked before the call),
- tools: the complete list of tools it gets (least privilege: diagnostics can't propose,
  communications can't touch infrastructure, nobody can execute),
- transitions: the only lifecycle moves its tools may make (checked in lifecycle.transition),
- results: the typed models it submits its output as (validated by pydantic in its tools),
- postcondition: what must be true when it returns (checked after the call),
- needs: incident fields that must exist before it is dispatched (remediation needs a diagnosis).

A dispatch outside acts_in, or a return that breaks the postcondition, is refused and recorded as
a `contract.violation` event. PLATFORM_TRANSITIONS are the moves only platform code makes: after
an approval decision, around execution, and when an agent is unavailable.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from .lifecycle import ALLOWED_TRANSITIONS, Status
from .schemas import (
    DiagnosisResult,
    Postmortem,
    RemediationDecline,
    RemediationProposal,
    StatusUpdate,
    TriageResult,
    VerificationResult,
)
from .store import Record

S = Status
Transition = tuple[Status, Status]
# (incident before the call, incident after, events recorded during the call) -> problem or None
Postcondition = Callable[[Record, Record, list[Record]], str | None]

READ_INCIDENT = ("get_incident",)
OBSERVE = ("get_health_overview", "get_service_info", "get_metrics")


@dataclass(frozen=True)
class AgentContract:
    role: str
    purpose: str
    acts_in: frozenset[Status]
    tools: frozenset[str]
    transitions: frozenset[Transition]
    results: tuple[type[BaseModel], ...]
    postcondition: Postcondition
    needs: tuple[str, ...] = ()

    def check_dispatch(self, status: Status, incident: Record | None = None) -> str | None:
        if status not in self.acts_in:
            allowed = ", ".join(sorted(self.acts_in))
            return f"the {self.role} agent acts only on incidents that are {allowed}; this one is {status}"
        missing = [f for f in self.needs if incident is not None and not incident.get(f)]
        if missing:
            return f"the {self.role} agent needs the incident's {', '.join(missing)} first; this incident has none"
        return None

    def describe(self) -> dict:
        return {
            "role": self.role,
            "purpose": self.purpose,
            "acts_in": sorted(self.acts_in),
            "tools": sorted(self.tools),
            "transitions": sorted(f"{a} -> {b}" for a, b in self.transitions),
            "results": [m.__name__ for m in self.results],
            "needs": list(self.needs),
        }


def _kinds(events: list[Record]) -> set[str]:
    return {e["kind"] for e in events}


def _triage_done(before: Record, after: Record, events: list[Record]) -> str | None:
    if after["status"] == S.INVESTIGATING and after.get("triage"):
        return None
    if "triage.inconclusive" in _kinds(events):
        return None
    return "triage returned without submitting a TriageResult or reporting that triage was inconclusive"


def _diagnosis_done(before: Record, after: Record, events: list[Record]) -> str | None:
    if "diagnosis.completed" in _kinds(events) and after.get("diagnosis"):
        return None
    return "diagnostics returned without submitting a DiagnosisResult"


def _remediation_done(before: Record, after: Record, events: list[Record]) -> str | None:
    if after["status"] != S.INVESTIGATING or "remediation.declined" in _kinds(events):
        return None
    if "remediation.denied" in _kinds(events):
        return "every proposal was denied by policy and remediation did not decline"
    return "remediation returned without a RemediationProposal or a RemediationDecline"


def _verification_done(before: Record, after: Record, events: list[Record]) -> str | None:
    if "verification.completed" in _kinds(events):
        return None
    return "verification returned without submitting a VerificationResult"


def _communications_done(before: Record, after: Record, events: list[Record]) -> str | None:
    kinds = _kinds(events)
    if before["status"] == S.VERIFYING and (before.get("verification") or {}).get("recovered"):
        if after["status"] != S.RESOLVED:
            return "the service is verified; communications must update stakeholders and submit a Postmortem"
        return None
    if not any(k.startswith("status_update.") for k in kinds):
        return "communications returned without posting a StatusUpdate"
    return None


CONTRACTS: dict[str, AgentContract] = {
    "triage": AgentContract(
        role="triage",
        purpose="Identify the affected service and severity.",
        acts_in=frozenset({S.TRIAGING}),
        tools=frozenset({*READ_INCIDENT, *OBSERVE, "submit_triage", "report_inconclusive_triage"}),
        transitions=frozenset({(S.TRIAGING, S.INVESTIGATING)}),
        results=(TriageResult,),
        postcondition=_triage_done,
    ),
    "diagnostics": AgentContract(
        role="diagnostics",
        purpose="Find the root cause, with evidence and a confidence.",
        acts_in=frozenset({S.INVESTIGATING}),
        tools=frozenset(
            {
                *READ_INCIDENT,
                *OBSERVE,
                "search_logs",
                "get_recent_deployments",
                "search_runbooks",
                "find_similar_incidents",
                "submit_diagnosis",
            }
        ),
        transitions=frozenset(),
        results=(DiagnosisResult,),
        postcondition=_diagnosis_done,
    ),
    "remediation": AgentContract(
        role="remediation",
        purpose="Propose one runbook action for the policy engine and a human; never execute.",
        acts_in=frozenset({S.INVESTIGATING}),
        tools=frozenset(
            {
                *READ_INCIDENT,
                "get_service_info",
                "get_metrics",
                "search_runbooks",
                "find_similar_incidents",
                "list_allowed_actions",
                "submit_proposal",
                "decline_remediation",
            }
        ),
        transitions=frozenset({(S.INVESTIGATING, S.AWAITING_APPROVAL)}),
        results=(RemediationProposal, RemediationDecline),
        postcondition=_remediation_done,
        needs=("diagnosis",),
    ),
    "verification": AgentContract(
        role="verification",
        purpose="Check whether the service recovered after the action ran.",
        acts_in=frozenset({S.VERIFYING}),
        tools=frozenset({*READ_INCIDENT, "get_health_overview", "get_metrics", "submit_verification"}),
        transitions=frozenset({(S.VERIFYING, S.FAILED)}),
        results=(VerificationResult,),
        postcondition=_verification_done,
    ),
    "communications": AgentContract(
        role="communications",
        purpose="Update stakeholders; write the postmortem and resolve a verified incident.",
        acts_in=frozenset(set(S) - {S.OPEN, S.RESOLVED}),
        tools=frozenset({*READ_INCIDENT, "get_incident_timeline", "post_status_update", "submit_postmortem"}),
        transitions=frozenset({(S.VERIFYING, S.RESOLVED)}),
        results=(StatusUpdate, Postmortem),
        postcondition=_communications_done,
    ),
}

COORDINATOR_TRANSITIONS: frozenset[Transition] = frozenset(
    {
        (S.OPEN, S.TRIAGING),
        (S.TRIAGING, S.ESCALATED),
        (S.INVESTIGATING, S.ESCALATED),
        (S.AWAITING_APPROVAL, S.ESCALATED),
        (S.FAILED, S.ESCALATED),
    }
)
COORDINATOR_TOOLS = frozenset({*READ_INCIDENT, "escalate_incident", *(f"ask_{role}" for role in CONTRACTS)})

PLATFORM_TRANSITIONS: frozenset[Transition] = frozenset(
    {
        (S.AWAITING_APPROVAL, S.REMEDIATING),  # a person (or the policy) approved
        (S.AWAITING_APPROVAL, S.ESCALATED),  # a person rejected
        (S.REMEDIATING, S.VERIFYING),  # the action ran
        (S.REMEDIATING, S.FAILED),  # the action failed
        # An agent stayed unavailable after every retry (dead letter):
        (S.TRIAGING, S.ESCALATED),
        (S.INVESTIGATING, S.ESCALATED),
        (S.VERIFYING, S.FAILED),
        (S.FAILED, S.ESCALATED),
    }
)


def transitions_of(actor: str) -> frozenset[Transition]:
    if actor == "platform":
        return PLATFORM_TRANSITIONS
    if actor == "coordinator":
        return COORDINATOR_TRANSITIONS
    contract = CONTRACTS.get(actor)
    return contract.transitions if contract else frozenset()


def may_transition(actor: str, current: Status, target: Status) -> bool:
    return (current, target) in transitions_of(actor)


def owners(current: Status, target: Status) -> list[str]:
    actors = ["platform", "coordinator", *CONTRACTS]
    return [a for a in actors if may_transition(a, current, target)]


def unowned_transitions() -> list[Transition]:
    """Legal moves nobody may make (should be empty)."""
    return [(a, b) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets if not owners(a, b)]
