"""The shared state every agent reads and writes: incidents, approvals, the audit log, services.

Records are plain dicts so they serialize the same way to SQLite and DynamoDB, and to agents.

Two rules every backend enforces:

- An incident's status changes only through `transition_incident`, a compare-and-set on the
  current status. `opsrelay.lifecycle.transition` is the only caller; `update_incident` refuses
  status changes and is itself a compare-and-set, so it can never overwrite a concurrent move.
- The audit log is a hash chain per incident: each event stores the hash of the one before it,
  so editing or deleting an event breaks every hash after it (see `opsrelay.audit`).
"""

import hashlib
import json
import uuid
from abc import ABC, abstractmethod
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


class Store(ABC):
    # Incidents
    @abstractmethod
    def put_incident(self, incident: Record) -> None:
        """Create an incident. Never used to change an existing incident's status."""

    @abstractmethod
    def get_incident(self, incident_id: str) -> Record | None: ...

    @abstractmethod
    def list_incidents(self, limit: int = 50) -> list[Record]:
        """Newest first."""

    @abstractmethod
    def transition_incident(self, incident_id: str, from_status: str, updates: Record) -> Record | None:
        """Apply `updates` only if the incident is still in `from_status` (atomic).

        Returns the updated record, or None if the status had changed.
        """

    # Audit log (append-only hash chain)
    @abstractmethod
    def append_event(self, event: Record) -> Record:
        """Seal the event onto its incident's chain (atomically) and store it. Returns it sealed."""

    @abstractmethod
    def list_events(self, incident_id: str) -> list[Record]:
        """Oldest first."""

    # Approvals
    @abstractmethod
    def put_approval(self, approval: Record) -> None: ...

    @abstractmethod
    def get_approval(self, approval_id: str) -> Record | None: ...

    @abstractmethod
    def list_approvals(self, status: str | None = None, incident_id: str | None = None) -> list[Record]:
        """Oldest first."""

    @abstractmethod
    def transition_approval(self, approval_id: str, from_status: str, updates: Record) -> Record | None:
        """Apply `updates` only if the approval is still in `from_status`.

        Returns the updated record, or None if it was not in that status (someone else decided first).
        """

    # Executions (idempotency)
    @abstractmethod
    def claim_execution(self, execution: Record) -> bool:
        """Store the execution keyed by execution["key"] unless that key exists. True if claimed."""

    @abstractmethod
    def get_execution(self, key: str) -> Record | None: ...

    @abstractmethod
    def finish_execution(self, key: str, updates: Record) -> Record: ...

    # Dead letters
    @abstractmethod
    def put_dead_letter(self, dead_letter: Record) -> None: ...

    @abstractmethod
    def list_dead_letters(self, limit: int = 50) -> list[Record]:
        """Newest first."""

    # Simulated environment
    @abstractmethod
    def put_service(self, service: Record) -> None: ...

    @abstractmethod
    def get_service(self, name: str) -> Record | None: ...

    @abstractmethod
    def list_services(self) -> list[Record]: ...

    # Helpers shared by every backend
    def update_incident(self, incident_id: str, **fields: Any) -> Record:
        """Change an incident's fields, but never its status (use opsrelay.lifecycle.transition)."""
        if "status" in fields:
            raise StatusChangeError("Incident status changes go through opsrelay.lifecycle.transition()")
        for _ in range(5):
            incident = self.get_incident(incident_id)
            if incident is None:
                raise KeyError(f"Unknown incident {incident_id}")
            updated = self.transition_incident(incident_id, incident["status"], {**fields, "updated_at": now_iso()})
            if updated is not None:
                return updated
        raise RuntimeError(f"{incident_id} kept changing status; update not applied")

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
        event = {
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
        return self.append_event(event)
