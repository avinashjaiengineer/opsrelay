"""The shared state every agent reads and writes: incidents, approvals, the audit log, services.

Records are plain dicts so they serialize the same way to SQLite and DynamoDB, and to agents.
"""

import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

Record = dict[str, Any]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_incident_id() -> str:
    return f"inc-{uuid.uuid4().hex[:10]}"


def new_approval_id() -> str:
    return f"apr-{uuid.uuid4().hex[:10]}"


class Store(ABC):
    # Incidents
    @abstractmethod
    def put_incident(self, incident: Record) -> None: ...

    @abstractmethod
    def get_incident(self, incident_id: str) -> Record | None: ...

    @abstractmethod
    def list_incidents(self, limit: int = 50) -> list[Record]:
        """Newest first."""

    # Audit log (append-only)
    @abstractmethod
    def append_event(self, event: Record) -> None: ...

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

    # Simulated environment
    @abstractmethod
    def put_service(self, service: Record) -> None: ...

    @abstractmethod
    def get_service(self, name: str) -> Record | None: ...

    @abstractmethod
    def list_services(self) -> list[Record]: ...

    # Helpers shared by every backend
    def update_incident(self, incident_id: str, **fields: Any) -> Record:
        incident = self.get_incident(incident_id)
        if incident is None:
            raise KeyError(f"Unknown incident {incident_id}")
        incident.update(fields)
        incident["updated_at"] = now_iso()
        self.put_incident(incident)
        return incident

    def record(
        self,
        incident_id: str | None,
        actor: str,
        kind: str,
        message: str,
        data: Record | None = None,
    ) -> Record:
        event = {
            "id": uuid.uuid4().hex,
            "incident_id": incident_id or "-",
            "actor": actor,
            "kind": kind,
            "message": message,
            "data": data or {},
            "created_at": now_iso(),
        }
        self.append_event(event)
        return event
