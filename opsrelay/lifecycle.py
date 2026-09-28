"""The incident lifecycle: every status an incident can have and the only legal moves between them.

    OPEN -> TRIAGING -> INVESTIGATING -> AWAITING_APPROVAL -> REMEDIATING -> VERIFYING -> RESOLVED
                 |             |                 |                 |             |
                 +-------------+-----------------+                 +-> FAILED <--+
                               v                                         |
                           ESCALATED <-----------------------------------+

Every status change goes through `transition()`. It checks the move against
ALLOWED_TRANSITIONS and against the acting agent's contract (who may make which move), then
writes it with a compare-and-set on the current status, so two processes can never both move
the same incident. Stores refuse status changes made any other way.
"""

from enum import StrEnum

from .store import Record, Store, now_iso


class Status(StrEnum):
    OPEN = "open"
    TRIAGING = "triaging"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    REMEDIATING = "remediating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    FAILED = "failed"
    ESCALATED = "escalated"


ALLOWED_TRANSITIONS: dict[Status, set[Status]] = {
    Status.OPEN: {Status.TRIAGING},
    Status.TRIAGING: {Status.INVESTIGATING, Status.ESCALATED},
    Status.INVESTIGATING: {Status.AWAITING_APPROVAL, Status.ESCALATED},
    Status.AWAITING_APPROVAL: {Status.REMEDIATING, Status.ESCALATED},
    Status.REMEDIATING: {Status.VERIFYING, Status.FAILED},
    Status.VERIFYING: {Status.RESOLVED, Status.FAILED},
    Status.FAILED: {Status.ESCALATED},
}

TERMINAL = frozenset(s for s in Status if s not in ALLOWED_TRANSITIONS)  # RESOLVED, ESCALATED


class IllegalTransition(ValueError):
    pass


def status_of(incident: Record) -> Status:
    try:
        return Status(incident["status"])
    except ValueError as e:
        raise IllegalTransition(f"{incident['id']} has unknown status '{incident['status']}'") from e


def is_allowed(current: Status, target: Status) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def transition(store: Store, incident_id: str, to: Status, *, actor: str, reason: str = "", **fields) -> Record:  # noqa: ANN003
    """Move an incident to `to`, or raise IllegalTransition.

    `actor` is the agent role or "platform"; its contract must own the move. Extra `fields` are
    written in the same atomic update.
    """
    from .contracts import may_transition  # contracts import lifecycle

    incident = store.get_incident(incident_id)
    if incident is None:
        raise KeyError(f"Unknown incident {incident_id}")
    current = status_of(incident)
    if not is_allowed(current, to):
        allowed = ", ".join(sorted(ALLOWED_TRANSITIONS.get(current, set()))) or "none (terminal)"
        raise IllegalTransition(f"{incident_id} is {current}; it cannot move to {to} (allowed: {allowed})")
    if not may_transition(actor, current, to):
        raise IllegalTransition(f"{actor} may not move {incident_id} from {current} to {to}")
    updated = store.transition_incident(incident_id, current, {**fields, "status": str(to), "updated_at": now_iso()})
    if updated is None:
        raise IllegalTransition(f"{incident_id} changed status while moving from {current} to {to}; try again")
    store.record(
        incident_id,
        actor,
        "status.changed",
        f"{current} -> {to}" + (f": {reason}" if reason else ""),
        {"from": str(current), "to": str(to)},
    )
    if to is Status.ESCALATED:
        _cancel_pending_approvals(store, incident_id, reason)
    return updated


def _cancel_pending_approvals(store: Store, incident_id: str, reason: str) -> None:
    for approval in store.list_approvals(status="pending", incident_id=incident_id):
        if store.transition_approval(approval["id"], "pending", {"status": "cancelled", "note": reason or None}):
            store.record(incident_id, "platform", "approval.cancelled", f"{approval['action']} cancelled: escalated")
