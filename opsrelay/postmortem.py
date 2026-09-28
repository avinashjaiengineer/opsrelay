"""Postmortems for every closed incident, as Markdown.

A resolved incident has the postmortem the communications agent wrote (from the record and its
timeline, validated as a `Postmortem`). An escalated one has none, because people finished it, so
`render` drafts one from the record: what was found, what was proposed and decided, why it went to
a person, and the runbook's follow-ups, for the owning team to complete. Either way the document
ends with the similar past incidents from incident memory and the audit chain's head, so it can be
checked against the record.
"""

from . import memory
from .audit import verify_incident
from .lifecycle import TERMINAL
from .schemas import Postmortem
from .store import Record, Store

NOISE = ("tool.", "status.changed", "delegation.", "job.")


def _draft(store: Store, incident: Record) -> Postmortem:
    diagnosis = incident.get("diagnosis") or {}
    approvals = store.list_approvals(incident_id=incident["id"])
    events = [e for e in store.list_events(incident["id"]) if not e["kind"].startswith(NOISE)]
    decided = [
        f"{a['action']} on {a['service']} ({a['risk']} risk): {a['status']}"
        + (f" by {a['decided_by']}" if a.get("decided_by") else "")
        + (f", note: {a['note']}" if a.get("note") else "")
        for a in approvals
    ]
    factors = [f"Diagnosis confidence {diagnosis['confidence']:.0%}"] if diagnosis.get("confidence") else []
    factors += [f"Proposal {d}" for d in decided]
    return Postmortem(
        incident_id=incident["id"],
        summary=f"{incident['title']}. The incident was {incident['status']}"
        + (f": {incident['escalation_reason']}" if incident.get("escalation_reason") else "."),
        impact=incident.get("customer_impact")
        or f"{incident.get('severity') or 'Unknown severity'} on "
        f"{incident.get('service') or 'an unidentified service'}.",
        timeline=[f"{e['created_at'][11:19]} {e['actor']}: {e['message'][:200]}" for e in events][:20]
        or [f"{incident['created_at'][11:19]} opened"],
        root_cause=incident.get("root_cause") or "Not determined by the agents; to be completed by the owning team.",
        resolution=f"Handed to a person ({incident['status']}). To be completed by the owning team.",
        contributing_factors=factors,
        detection=f"Detected by a {incident.get('source', 'manual')} alert: {incident['title']}.",
        action_items=["Complete this postmortem: root cause, resolution and follow-ups"]
        + (["Review why the proposed remediation was rejected"] if any("rejected" in d for d in decided) else []),
    )


def render(store: Store, incident_id: str) -> Record:
    """{"markdown": ..., "generated": bool}: the agent's postmortem, or a draft for a closed incident."""
    incident = store.get_incident(incident_id)
    if incident is None:
        raise KeyError(f"Unknown incident {incident_id}")
    if incident["status"] not in TERMINAL:
        raise ValueError(f"{incident_id} is {incident['status']}; a postmortem is written once it is closed")
    generated = not incident.get("postmortem")
    text = _draft(store, incident).markdown(incident["title"]) if generated else incident["postmortem"]
    if generated:
        text = text.replace(
            "\n\n", "\n\n> Draft generated from the incident record; the agents did not resolve it.\n\n", 1
        )

    similar = memory.similar(store, incident)
    if similar:
        text += "\n## Similar past incidents\n\n" + "\n".join(
            f"- {m['incident_id']} ({m['similarity']:.0%} similar): {m['title']}; "
            f"{m.get('action') or 'no action'} -> {m['outcome']}"
            for m in similar
        )
        text += "\n"
    audit = verify_incident(store, incident_id)
    text += (
        f"\n---\nAudit chain: {'intact' if audit['ok'] else 'TAMPERED'}, {audit.get('events')} events"
        + (f", head {audit['head'][:16]}" if audit.get("head") else "")
        + "\n"
    )
    return {"incident_id": incident_id, "markdown": text, "generated": generated}
