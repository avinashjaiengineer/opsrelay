"""The dead-letter workflow: what happens when an agent stays unavailable.

    request -> retry -> retry -> dead-letter queue -> incident handed to a human

The failed request is kept with every attempt's error, so it can be inspected and replayed
(`opsrelay dlq`), and the incident is escalated through legal transitions only (VERIFYING has to
pass through FAILED first).
"""

import uuid

from .lifecycle import TERMINAL, IllegalTransition, Status, status_of, transition
from .store import Record, Store, now_iso
from .store.base import Commit, make_event, new_record


def dead_letter(store: Store, incident_id: str, agent: str, request: str, attempts: list[str], error: str) -> Record:
    letter = {
        "id": f"dlq-{uuid.uuid4().hex[:10]}",
        "incident_id": incident_id,
        "agent": agent,
        "request": request,
        "attempts": attempts,
        "error": error,
        "created_at": now_iso(),
    }
    store.commit(
        Commit(
            new_records=[new_record("dead_letter", letter["id"], "open", **letter)],
            events=[
                make_event(
                    incident_id,
                    "platform",
                    "agent.unavailable",
                    f"{agent} unavailable after {len(attempts)} attempt(s): {error}. "
                    "Request moved to the dead-letter queue.",
                    {"dead_letter_id": letter["id"], "agent": agent, "attempts": attempts},
                )
            ],
        )
    )
    _hand_to_human(store, incident_id, f"the {agent} agent is unavailable ({letter['id']})")
    return letter


def _hand_to_human(store: Store, incident_id: str, reason: str) -> None:
    incident = store.get_incident(incident_id)
    if incident is None or status_of(incident) in TERMINAL:
        return
    try:
        if status_of(incident) is Status.VERIFYING:
            transition(store, incident_id, Status.FAILED, actor="platform", reason=reason, failure_reason=reason)
        transition(store, incident_id, Status.ESCALATED, actor="platform", reason=reason, escalation_reason=reason)
    except IllegalTransition:
        # e.g. AWAITING_APPROVAL or REMEDIATING: a person or the running action decides next.
        store.record(incident_id, "platform", "escalation.deferred", f"Left in {incident['status']}: {reason}")
