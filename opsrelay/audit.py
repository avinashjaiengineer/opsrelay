"""Tamper evidence for the audit log.

Every event carries `prev_hash` (the hash of the event before it for the same incident) and
`hash` (SHA-256 of the event itself, including `prev_hash`). Editing, deleting or reordering an
event changes a hash that a later event depends on, so `verify_chain` pinpoints the first event
that no longer matches.
"""

from .store import Record, Store
from .store.base import GENESIS_HASH, seal


def verify_chain(events: list[Record]) -> Record:
    """{"ok": True, "events": n} or {"ok": False, "at": index, "event_id": ..., "reason": ...}."""
    prev = GENESIS_HASH
    for i, event in enumerate(events):
        if event.get("prev_hash") != prev:
            return {"ok": False, "at": i, "event_id": event.get("id"), "reason": "prev_hash does not match the chain"}
        expected = seal({k: v for k, v in event.items() if k not in ("hash", "prev_hash")}, prev)["hash"]
        if event.get("hash") != expected:
            return {
                "ok": False,
                "at": i,
                "event_id": event.get("id"),
                "reason": "event content does not match its hash",
            }
        prev = event["hash"]
    return {"ok": True, "events": len(events), "head": prev}


def verify_incident(store: Store, incident_id: str) -> Record:
    return {"incident_id": incident_id, **verify_chain(store.list_events(incident_id))}
