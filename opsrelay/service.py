"""Operations behind the coordinator runtime's entrypoint and the CLI."""

import logging

from . import approvals
from .agents import Invoker, build_coordinator
from .config import SPECIALISTS, Role, get_settings
from .environment import SCENARIOS, Environment, SimulatedEnvironment, get_environment
from .store import Record, Store, get_store, new_incident_id, now_iso

log = logging.getLogger(__name__)


def configured_invokers() -> dict[Role, Invoker] | None:
    """A2A invokers when specialists are remote; None means run them in-process."""
    settings = get_settings()
    if settings.specialist_transport == "local":
        return None
    from .remote import a2a_invoker

    return {role: a2a_invoker(role, settings.endpoint_for(role), settings.a2a_timeout_seconds) for role in SPECIALISTS}


def new_incident_prompt(incident: Record) -> str:
    return (
        f"New incident {incident['id']}. Coordinate the response.\n"
        f"<alert>\n{incident['title']}\n{incident['description']}\n</alert>"
    )


def decision_prompt(approval: Record) -> str:
    detail = (approval.get("result") or {}).get("detail", "")
    note = f" Note: {approval['note']}" if approval.get("note") else ""
    return (
        f"Approval {approval['id']} on incident {approval['incident_id']} was decided by {approval['decided_by']}: "
        f"{approval['action']} on {approval['service']} is now {approval['status']}. {detail}{note} "
        "Continue the response."
    )


class IncidentService:
    def __init__(
        self,
        store: Store | None = None,
        env: Environment | None = None,
        invokers: dict[Role, Invoker] | None = None,
    ):
        self.store = store or get_store()
        self.env = env or get_environment(self.store)
        self.invokers = invokers if invokers is not None else configured_invokers()

    def run_coordinator(self, incident_id: str, prompt: str) -> str:
        agent = build_coordinator(self.store, self.env, self.invokers)
        try:
            report = str(agent(prompt)).strip()
        except Exception as e:
            log.exception("coordinator failed on %s", incident_id)
            self.store.record(incident_id, "coordinator", "error", f"{type(e).__name__}: {e}")
            raise
        self.store.record(incident_id, "coordinator", "report", report)
        return report

    def open_incident(
        self,
        title: str,
        description: str = "",
        *,
        source: str = "manual",
        service: str | None = None,
        external_ref: str | None = None,
        run: bool = True,
    ) -> Record:
        if not title.strip():
            raise ValueError("title is required")
        if external_ref:
            for existing in self.store.list_incidents(limit=200):
                if existing.get("external_ref") == external_ref and existing["status"] != "resolved":
                    return {"incident": existing, "report": None, "deduplicated": True}
        created = now_iso()
        incident = {
            "id": new_incident_id(),
            "title": title.strip()[:300],
            "description": description[:5000],
            "source": source,
            "external_ref": external_ref,
            "service": service,
            "severity": None,
            "status": "open",
            "created_at": created,
            "updated_at": created,
        }
        self.store.put_incident(incident)
        self.store.record(incident["id"], f"source:{source}", "incident.opened", incident["title"])
        report = None
        if run:
            report = self.run_coordinator(incident["id"], new_incident_prompt(incident))
        return {"incident": self.store.get_incident(incident["id"]), "report": report}

    def decide_approval(
        self, approval_id: str, *, approve: bool, approver: str, note: str | None = None, run: bool = True
    ) -> Record:
        approval = approvals.decide(self.store, self.env, approval_id, approve=approve, approver=approver, note=note)
        report = None
        if run:
            report = self.run_coordinator(approval["incident_id"], decision_prompt(approval))
        return {"approval": approval, "incident": self.store.get_incident(approval["incident_id"]), "report": report}

    def simulate(self, scenario: str, *, open_incident: bool = True, run: bool = True) -> Record:
        """Inject a fault into the simulated environment and (by default) raise its alert as an incident."""
        if scenario not in SCENARIOS:
            raise ValueError(f"Unknown scenario '{scenario}'. Choose from: {', '.join(SCENARIOS)}")
        if not isinstance(self.env, SimulatedEnvironment):
            raise ValueError("Scenarios need the simulated environment")
        alert = self.env.inject(scenario)
        if not open_incident:
            return {"alert": alert}
        return {
            "alert": alert,
            **self.open_incident(alert["title"], alert["description"], source="alertmanager", run=run),
        }

    def get_incident(self, incident_id: str) -> Record:
        incident = self.store.get_incident(incident_id)
        if incident is None:
            raise KeyError(f"Unknown incident {incident_id}")
        return {
            "incident": incident,
            "approvals": self.store.list_approvals(incident_id=incident_id),
            "events": self.store.list_events(incident_id),
        }

    def list_incidents(self, limit: int = 50) -> list[Record]:
        return self.store.list_incidents(limit)

    def list_approvals(self, status: str | None = "pending") -> list[Record]:
        return self.store.list_approvals(status=status)

    def health(self) -> list[Record]:
        return self.env.health_overview()
