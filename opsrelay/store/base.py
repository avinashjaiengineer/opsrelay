"""The shared state every agent reads and writes: incidents, approvals, the audit log, services.

Records are plain dicts so they serialize the same way to SQLite and DynamoDB, and to agents.

Every write goes through `Store.commit`: one atomic transaction that can change an incident,
create or move approvals and records, and append audit events. So:

- An incident's status changes only by compare-and-set on its current status, and only through
  `opsrelay.lifecycle.transition`; `update_incident` refuses status changes.
- A state change and the audit events describing it are written together or not at all: the
  audit log can't miss a change that happened, or record one that didn't.
- The audit log is a hash chain per incident: each event stores the hash of the one before it,
  so editing or deleting an event breaks every hash after it (see `opsrelay.audit`).
- Records (executions, jobs, dead letters, policy versions) change by compare-and-set on their
  revision, which is how leases are taken and taken over.
"""

import hashlib
import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

Record = dict[str, Any]

GENESIS_HASH = "0" * 64
AGENT_ROLES = ("coordinator", "triage", "diagnostics", "remediation", "verification", "communications")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_incident_id() -> str:
    return f"inc-{uuid.uuid4().hex[:10]}"


def new_approval_id() -> str:
    return f"apr-{uuid.uuid4().hex[:10]}"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def seal(event: Record, prev_hash: str) -> Record:
    """Link an event to the previous one in its incident's chain and hash it."""
    body = {k: v for k, v in event.items() if k != "hash"}
    body["prev_hash"] = prev_hash
    return {**body, "hash": sha256(body)}


def actor_type(actor: str) -> str:
    if actor.startswith("source:"):
        return "source"
    if actor in AGENT_ROLES:
        return "agent"
    if actor == "platform" or actor.startswith("policy:"):
        return "platform"
    return "human"


class StatusChangeError(ValueError):
    pass


@dataclass(frozen=True)
class IncidentChange:
    """Change an incident's fields, but only if its status is still `expected_status`."""

    incident_id: str
    expected_status: str
    updates: Record


@dataclass(frozen=True)
class ApprovalMove:
    """Change an approval, but only if its status is still `expected_status`."""

    approval_id: str
    expected_status: str
    updates: Record


@dataclass(frozen=True)
class RecordMove:
    """Change a record, but only if its revision is still `expected_rev` (optimistic concurrency)."""

    kind: str
    record_id: str
    expected_rev: int
    updates: Record


@dataclass
class Commit:
    """Writes applied together or not at all. Events are sealed onto their incidents' hash chains
    in the same transaction, so a state change can never exist without its audit event."""

    incident: IncidentChange | None = None
    new_approvals: list[Record] = field(default_factory=list)
    approval_moves: list[ApprovalMove] = field(default_factory=list)
    new_records: list[Record] = field(default_factory=list)
    record_moves: list[RecordMove] = field(default_factory=list)
    events: list[Record] = field(default_factory=list)


@dataclass
class Committed:
    incident: Record | None
    approvals: dict[str, Record]
    records: dict[tuple[str, str], Record]
    events: list[Record]


def make_event(
    incident_id: str | None,
    actor: str,
    kind: str,
    message: str,
    data: Record | None = None,
    *,
    input: Any = None,  # noqa: A002 - mirrors the audit field names
    output: Any = None,
) -> Record:
    """An unsealed audit event, to be written with Store.commit or Store.record."""
    return {
        "id": uuid.uuid4().hex,
        "incident_id": incident_id or "-",
        "actor": actor,
        "actor_type": actor_type(actor),
        "kind": kind,
        "message": message,
        "data": data or {},
        "input_hash": sha256(input) if input is not None else None,
        "output_hash": sha256(output) if output is not None else None,
        "created_at": now_iso(),
    }


def new_record(kind: str, record_id: str, status: str, **doc: Any) -> Record:
    now = now_iso()
    created = doc.pop("created_at", None) or now
    return {**doc, "kind": kind, "id": record_id, "status": status, "rev": 0, "created_at": created, "updated_at": now}


class Store(ABC):
    """Backends implement the abstract methods; everything else is built on `commit`."""

    # Atomic writes
    @abstractmethod
    def commit(self, change: Commit) -> Committed | None:
        """Apply every write in `change` atomically. Returns None, and writes nothing, if any
        condition fails: an incident or approval not in its expected status, an approval or record
        that already exists, or a record whose revision moved on."""

    # Incidents
    @abstractmethod
    def put_incident(self, incident: Record) -> None:
        """Create an incident. Never used to change an existing incident's status."""

    @abstractmethod
    def get_incident(self, incident_id: str) -> Record | None: ...

    @abstractmethod
    def list_incidents(self, limit: int = 50) -> list[Record]:
        """Newest first."""

    # Audit log (append-only hash chain)
    @abstractmethod
    def list_events(self, incident_id: str) -> list[Record]:
        """Oldest first."""

    def event_count(self, incident_id: str) -> int:
        """How many events an incident's audit chain holds (stores override this cheaply)."""
        return len(self.list_events(incident_id))

    # Approvals
    @abstractmethod
    def get_approval(self, approval_id: str) -> Record | None: ...

    @abstractmethod
    def list_approvals(self, status: str | None = None, incident_id: str | None = None) -> list[Record]:
        """Oldest first."""

    # Records: executions, jobs, dead letters, policy versions
    @abstractmethod
    def get_record(self, kind: str, record_id: str) -> Record | None: ...

    @abstractmethod
    def list_records(self, kind: str, status: str | None = None, limit: int = 200) -> list[Record]:
        """Newest first."""

    # Simulated environment
    @abstractmethod
    def put_service(self, service: Record) -> None: ...

    @abstractmethod
    def get_service(self, name: str) -> Record | None: ...

    @abstractmethod
    def list_services(self) -> list[Record]: ...

    # Built on commit
    def transition_incident(self, incident_id: str, from_status: str, updates: Record) -> Record | None:
        done = self.commit(Commit(incident=IncidentChange(incident_id, from_status, updates)))
        return done.incident if done else None

    def append_event(self, event: Record) -> Record:
        done = self.commit(Commit(events=[event]))
        if done is None:
            raise RuntimeError(f"Could not append to the audit chain of {event['incident_id']}")
        return done.events[0]

    def record(
        self,
        incident_id: str | None,
        actor: str,
        kind: str,
        message: str,
        data: Record | None = None,
        *,
        input: Any = None,  # noqa: A002 - mirrors the audit field names
        output: Any = None,
    ) -> Record:
        return self.append_event(make_event(incident_id, actor, kind, message, data, input=input, output=output))

    def update_incident(self, incident_id: str, *, events: list[Record] | tuple = (), **fields: Any) -> Record:
        """Change an incident's fields, never its status (use opsrelay.lifecycle.transition), together
        with any audit events describing the change."""
        if "status" in fields:
            raise StatusChangeError("Incident status changes go through opsrelay.lifecycle.transition()")
        for _ in range(5):
            incident = self.get_incident(incident_id)
            if incident is None:
                raise KeyError(f"Unknown incident {incident_id}")
            change = IncidentChange(incident_id, incident["status"], {**fields, "updated_at": now_iso()})
            done = self.commit(Commit(incident=change, events=list(events)))
            if done is not None:
                return done.incident
        raise RuntimeError(f"{incident_id} kept changing status; update not applied")

    def put_approval(self, approval: Record) -> None:
        if self.commit(Commit(new_approvals=[approval])) is None:
            raise ValueError(f"Approval {approval['id']} already exists")

    def transition_approval(self, approval_id: str, from_status: str, updates: Record) -> Record | None:
        done = self.commit(Commit(approval_moves=[ApprovalMove(approval_id, from_status, updates)]))
        return done.approvals[approval_id] if done else None

    def put_record(self, record: Record) -> bool:
        """Create a record unless one with the same kind and id exists. True if created."""
        return self.commit(Commit(new_records=[record])) is not None

    def move_record(self, kind: str, record_id: str, expected_rev: int, updates: Record) -> Record | None:
        done = self.commit(Commit(record_moves=[RecordMove(kind, record_id, expected_rev, updates)]))
        return done.records[(kind, record_id)] if done else None

    # Dead letters, as records
    def put_dead_letter(self, dead_letter: Record) -> None:
        self.put_record(new_record("dead_letter", dead_letter["id"], "open", **dead_letter))

    def list_dead_letters(self, limit: int = 50) -> list[Record]:
        return self.list_records("dead_letter", limit=limit)
