"""Copy a SQLite store into another store (DynamoDB), keeping every audit chain verifiable.

    opsrelay migrate --from opsrelay.db --to-table opsrelay

Incidents, approvals, records and services are copied as they are. Audit events are appended to
the target in their original order, and each store seals an event over the same fields with the
previous event's hash, so every chain ends at the same head hash; the copy is checked against
the source per chain. Anything already in the target is skipped, so a migration can be rerun.
"""

import logging

from .base import Commit, Record, Store
from .sqlite import SqliteStore

log = logging.getLogger(__name__)
CHUNK = 40  # events per transaction (DynamoDB allows 100 items, and each touches its chain item)


def _unsealed(event: Record) -> Record:
    return {k: v for k, v in event.items() if k not in ("hash", "prev_hash")}


def migrate(source: SqliteStore, target: Store) -> Record:
    conn = source._conn  # noqa: SLF001 - reading the source's tables directly is the point
    copied = {"incidents": 0, "approvals": 0, "records": 0, "services": 0, "events": 0, "chains": 0}
    mismatched: list[str] = []

    for svc in source.list_services():
        target.put_service(svc)
        copied["services"] += 1

    for (incident_id,) in conn.execute("SELECT id FROM incidents").fetchall():
        if target.get_incident(incident_id) is None:
            target.put_incident(source.get_incident(incident_id))
            copied["incidents"] += 1

    for approval in source.list_approvals():
        if target.get_approval(approval["id"]) is None and target.commit(Commit(new_approvals=[approval])):
            copied["approvals"] += 1

    for (kind,) in conn.execute("SELECT DISTINCT kind FROM records").fetchall():
        for record in source.list_records(kind, limit=1_000_000):
            if target.get_record(kind, record["id"]) is None and target.commit(Commit(new_records=[record])):
                copied["records"] += 1

    chains = [row[0] for row in conn.execute("SELECT DISTINCT incident_id FROM events").fetchall()]
    for chain in chains:
        events = source.list_events(chain)
        already = len(target.list_events(chain))
        pending = events[already:]
        for i in range(0, len(pending), CHUNK):
            if target.commit(Commit(events=[_unsealed(e) for e in pending[i : i + CHUNK]])) is None:
                raise RuntimeError(f"could not append events to {chain}")
            copied["events"] += len(pending[i : i + CHUNK])
        copied["chains"] += 1
        want = events[-1]["hash"] if events else None
        got = (target.list_events(chain) or [{}])[-1].get("hash")
        if want != got:
            mismatched.append(chain)
    return {"copied": copied, "chains_verified": len(chains) - len(mismatched), "mismatched": mismatched}
