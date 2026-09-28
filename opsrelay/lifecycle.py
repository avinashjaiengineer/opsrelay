"""The incident lifecycle: every status an incident can have and the only legal moves between them.

    OPEN -> TRIAGING -> INVESTIGATING -> AWAITING_APPROVAL -> REMEDIATING -> VERIFYING -> RESOLVED
                 |             |                 |                 |             |
                 +-------------+-----------------+                 +-> FAILED <--+
                               v                                         |
                           ESCALATED <-----------------------------------+

Every status change goes through `transition()`. It checks the move against
ALLOWED_TRANSITIONS and against the acting agent's contract (who may make which move), then
writes it with a compare-and-set on the current status, in the same atomic commit as its audit
events, so two processes can never both move the same incident, and no move is ever missing from
the audit log. Stores refuse status changes made any other way.
"""

from enum import StrEnum

from .store import Record, Store, now_iso
from .store.base import ApprovalMove, Commit, IncidentChange, make_event


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


def transition(
    store: Store,
    incident_id: str,
    to: Status,
    *,
    actor: str,
    reason: str = "",
    events: list[Record] | tuple = (),
    new_approvals: list[Record] | tuple = (),
    approval_moves: list[ApprovalMove] | tuple = (),
    **fields,  # noqa: ANN003
) -> Record:
    """Move an incident to `to`, or raise IllegalTransition.

    `actor` is the agent role or "platform"; its contract must own the move. Everything is one
    atomic commit: the status change and extra `fields`, a `status.changed` audit event, the
    caller's own `events`, and any approvals created or moved with it. Escalating also cancels
    pending approvals in the same commit.
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

    moves = list(approval_moves)
    all_events = [
        make_event(
            incident_id,
            actor,
            "status.changed",
            f"{current} -> {to}" + (f": {reason}" if reason else ""),
            {"from": str(current), "to": str(to)},
        ),
        *events,
    ]
    if to is Status.ESCALATED:
        moved = {m.approval_id for m in moves}
        for approval in store.list_approvals(status="pending", incident_id=incident_id):
            if approval["id"] not in moved:
                moves.append(ApprovalMove(approval["id"], "pending", {"status": "cancelled", "note": reason or None}))
                all_events.append(
                    make_event(
                        incident_id, "platform", "approval.cancelled", f"{approval['action']} cancelled: escalated"
                    )
                )
    now = now_iso()
    done = store.commit(
        Commit(
            incident=IncidentChange(
                incident_id, str(current), {**fields, "status": str(to), "status_since": now, "updated_at": now}
            ),
            new_approvals=list(new_approvals),
            approval_moves=moves,
            events=all_events,
        )
    )
    if done is None:
        raise IllegalTransition(
            f"{incident_id} or its approvals changed while moving from {current} to {to}; try again"
        )
    _measure(incident, current, to, now)
    return done.incident


def _measure(incident: Record, current: Status, to: Status, now: str) -> None:
    from datetime import datetime

    from . import telemetry

    since = incident.get("status_since") or incident["created_at"]
    seconds = (datetime.fromisoformat(now) - datetime.fromisoformat(since)).total_seconds()
    telemetry.observe("stage_duration_seconds", seconds, stage=str(current))
    if to in TERMINAL:
        telemetry.count("incidents_closed_total", outcome=str(to))
    if current is Status.VERIFYING and to is Status.FAILED:
        telemetry.count("verification_failures_total")
