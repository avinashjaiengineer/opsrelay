"""Incident memory: what happened last time, so agents and people can learn from it.

When an incident closes (resolved or escalated), the coordinator writes a memory record (kind
"memory", one per incident): the symptoms, service, category, root cause, the evidence, the action
taken and its outcome, approvals people rejected and why, how long it took, the runbook followed and
the postmortem's lessons, with an embedding of the symptoms (opsrelay.knowledge).

`similar` finds the closest past incidents to an incident: semantic similarity of the symptoms plus
a boost for the same service and the same failure category. The diagnostics and remediation agents
see them through the `find_similar_incidents` tool, and the dashboard shows them on the incident.
`recover` backfills memories for closed incidents that don't have one yet.
"""

import logging
from datetime import datetime

from . import knowledge
from .lifecycle import TERMINAL
from .store import Record, Store
from .store.base import new_record

log = logging.getLogger(__name__)
KIND = "memory"
SCAN_LIMIT = 500  # memories compared per query (newest first)
# An incident's "similar past incidents" must clear this: unrelated incidents share generic words
# (service, latency, error), so they score up to ~0.3; the same failure scores well above.
SIMILAR_MIN_SCORE = 0.4


def symptoms_text(incident: Record) -> str:
    """What an incident looked like when it arrived and was diagnosed: the text we compare."""
    diagnosis = incident.get("diagnosis") or {}
    evidence = "; ".join(str(e.get("value", "")) for e in diagnosis.get("evidence", [])[:6])
    parts = [
        incident.get("title", ""),
        incident.get("description", ""),
        f"service {incident['service']}" if incident.get("service") else "",
        diagnosis.get("root_cause", ""),
        evidence,
    ]
    return "\n".join(p for p in parts if p)


def _minutes(start: str | None, end: str | None) -> float | None:
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60, 1)
    except (TypeError, ValueError):
        return None


def build(store: Store, incident: Record) -> Record:
    approvals = store.list_approvals(incident_id=incident["id"])
    done = next((a for a in approvals if a["status"] in ("executed", "failed")), None)
    diagnosis = incident.get("diagnosis") or {}
    postmortem = incident.get("postmortem_data") or {}
    text = symptoms_text(incident)
    embedder, vector = knowledge.embed(text)
    return new_record(
        KIND,
        incident["id"],
        str(incident["status"]),
        incident_id=incident["id"],
        title=incident.get("title"),
        service=incident.get("service"),
        severity=incident.get("severity"),
        category=incident.get("category"),
        root_cause=incident.get("root_cause"),
        confidence=diagnosis.get("confidence"),
        evidence=[e.get("value") for e in diagnosis.get("evidence", [])][:6],
        action=done and done["action"],
        action_params=done and done.get("params"),
        action_result=done and (done.get("result") or {}).get("detail"),
        runbook_id=done and done.get("runbook_id"),
        outcome=str(incident["status"]),
        escalation_reason=incident.get("escalation_reason"),
        rejected=[
            {"action": a["action"], "by": a.get("decided_by"), "note": a.get("note")}
            for a in approvals
            if a["status"] in ("rejected", "denied")
        ],
        duration_minutes=_minutes(incident.get("created_at"), incident.get("updated_at")),
        summary=postmortem.get("summary"),
        lessons=postmortem.get("action_items", []),
        text=text,
        embedder=embedder,
        embedding=list(vector),
        created_at=incident.get("created_at"),
    )


def remember(store: Store, incident_id: str) -> Record | None:
    """Write the memory of a closed incident (once). Returns it, or None if the incident is still open
    or already remembered."""
    incident = store.get_incident(incident_id)
    if incident is None or incident["status"] not in TERMINAL:
        return None
    if store.get_record(KIND, incident_id) is not None:
        return None
    memory = build(store, incident)
    return memory if store.put_record(memory) else None


def remember_quietly(store: Store, incident_id: str) -> None:
    """For callers that must not fail because of memory (it can be backfilled by recover)."""
    try:
        remember(store, incident_id)
    except Exception:  # noqa: BLE001
        log.exception("could not write the memory of %s", incident_id)


def backfill(store: Store, limit: int = 200) -> list[str]:
    written = []
    for incident in store.list_incidents(limit=limit):
        if incident["status"] in TERMINAL and remember(store, incident["id"]):
            written.append(incident["id"])
    return written


def _vector(memory: Record, embedder: str) -> tuple[float, ...]:
    if memory.get("embedder") == embedder and memory.get("embedding"):
        return tuple(memory["embedding"])
    return knowledge.embed(memory.get("text", ""), embedder)[1]  # embedded another way: re-embed


def search(
    store: Store,
    text: str,
    *,
    service: str | None = None,
    category: str | None = None,
    exclude: str | None = None,
    k: int = 3,
    min_score: float = 0.05,
) -> list[Record]:
    embedder, query = knowledge.embed(text)
    scored = []
    for memory in store.list_records(KIND, limit=SCAN_LIMIT):
        if memory["id"] == exclude:
            continue
        score = 0.7 * knowledge.cosine(query, _vector(memory, embedder))
        if service and memory.get("service") == service:
            score += 0.15
        if category and category != "unknown" and memory.get("category") == category:
            score += 0.15
        if score >= min_score:
            scored.append((score, memory))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [public(m, s) for s, m in scored[:k]]


def similar(store: Store, incident: Record, k: int = 3) -> list[Record]:
    return search(
        store,
        symptoms_text(incident),
        service=incident.get("service"),
        category=incident.get("category"),
        exclude=incident["id"],
        k=k,
        min_score=SIMILAR_MIN_SCORE,
    )


def public(memory: Record, score: float | None = None) -> Record:
    keys = (
        "incident_id",
        "title",
        "service",
        "severity",
        "category",
        "root_cause",
        "action",
        "action_result",
        "runbook_id",
        "outcome",
        "escalation_reason",
        "rejected",
        "duration_minutes",
        "summary",
        "lessons",
        "created_at",
    )
    out = {key: memory.get(key) for key in keys}
    if score is not None:
        out["similarity"] = round(score, 3)
    return out
