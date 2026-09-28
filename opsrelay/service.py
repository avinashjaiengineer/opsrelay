"""Operations behind the coordinator runtime's entrypoint and the CLI."""

import logging

from . import approvals, memory, postmortem, runbooks, telemetry
from .agents import Invoker, build_coordinator
from .audit import verify_incident
from .config import SPECIALISTS, Role, get_settings
from .contracts import CONTRACTS, COORDINATOR_TOOLS, COORDINATOR_TRANSITIONS, PLATFORM_TRANSITIONS
from .environment import SCENARIOS, Environment, SimulatedEnvironment, get_environment
from .lifecycle import ALLOWED_TRANSITIONS, TERMINAL, Status
from .policy import Facts, get_policy
from .schemas import RemediationProposal
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
            with telemetry.span("opsrelay.coordinate", incident_id=incident_id):
                report = str(agent(prompt)).strip()
        except Exception as e:
            log.exception("coordinator failed on %s", incident_id)
            self.store.record(incident_id, "coordinator", "error", f"{type(e).__name__}: {e}")
            raise
        self.store.record(incident_id, "coordinator", "report", report)
        memory.remember_quietly(self.store, incident_id)  # if it closed; recover backfills misses
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
        incident_id: str | None = None,
        alert: Record | None = None,
        scenario: str | None = None,
    ) -> Record:
        """Open an incident. `incident_id` is given by alert intake, which has already deduplicated
        (see opsrelay.intake.router); `alert` is the normalized alert that raised it."""
        if not title.strip():
            raise ValueError("title is required")
        if external_ref and incident_id is None:
            for existing in self.store.list_incidents(limit=200):
                if existing.get("external_ref") == external_ref and existing["status"] not in TERMINAL:
                    return {"incident": existing, "report": None, "deduplicated": True}
        created = now_iso()
        incident = {
            "id": incident_id or new_incident_id(),
            "title": title.strip()[:300],
            "description": description[:5000],
            "source": source,
            "external_ref": external_ref,
            "service": service,
            "severity": None,
            "status": str(Status.OPEN),
            "status_since": created,
            "fingerprints": [external_ref] if external_ref else [],
            "alerts_count": 1,
            "alert": alert,
            "scenario": scenario,  # the simulated fault behind it, so it can be replayed (opsrelay.evals)
            "created_at": created,
            "updated_at": created,
        }
        self.store.put_incident(incident)
        telemetry.count("incidents_total", source=source)
        self.store.record(
            incident["id"],
            f"source:{source}",
            "incident.created",
            incident["title"],
            {"description": description[:500]},
        )
        report = None
        if run:
            report = self.run_coordinator(incident["id"], new_incident_prompt(incident))
        return {"incident": self.store.get_incident(incident["id"]), "report": report}

    def decide_approval(
        self,
        approval_id: str,
        *,
        approve: bool,
        approver: str,
        note: str | None = None,
        run: bool = True,
        role: str | None = None,
        verified: bool = False,
    ) -> Record:
        approval = approvals.decide(
            self.store,
            self.env,
            approval_id,
            approve=approve,
            approver=approver,
            note=note,
            role=role,
            verified=verified,
        )
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
            **self.open_incident(
                alert["title"], alert["description"], source="alertmanager", run=run, scenario=scenario
            ),
        }

    def ingest(self, message: object, *, queue: bool = True) -> list[Record]:
        """Route an alert message (CloudWatch alarm event or SNS notification, Alertmanager webhook):
        deduplicate, correlate, or open an incident. See opsrelay.intake."""
        from .intake import alerts, router

        parsed = alerts.parse(message, router.known_services(self))
        return [router.ingest(self, alert, queue=queue) for alert in parsed]

    def get_incident(self, incident_id: str) -> Record:
        incident = self.store.get_incident(incident_id)
        if incident is None:
            raise KeyError(f"Unknown incident {incident_id}")
        return {
            "incident": incident,
            "approvals": self.store.list_approvals(incident_id=incident_id),
            "events": self.store.list_events(incident_id),
            "similar": memory.similar(self.store, incident) if incident.get("service") else [],
        }

    def postmortem(self, incident_id: str) -> Record:
        return postmortem.render(self.store, incident_id)

    def similar_incidents(self, text: str = "", incident_id: str | None = None, k: int = 5) -> list[Record]:
        if incident_id:
            incident = self.store.get_incident(incident_id)
            if incident is None:
                raise KeyError(f"Unknown incident {incident_id}")
            return memory.similar(self.store, incident, k)
        if not text.strip():
            raise ValueError("give a description of the symptoms, or an incident id")
        return memory.search(self.store, text, k=k)

    def recover(self) -> Record:
        """Finish interrupted work: remediations, expired jobs, memories of closed incidents."""
        from . import jobs

        return {
            "remediations": approvals.recover(self.store, self.env),
            "requeued_jobs": jobs.requeue_expired(self.store),
            "memories": memory.backfill(self.store),
        }

    def list_incidents(self, limit: int = 50) -> list[Record]:
        return self.store.list_incidents(limit)

    def list_approvals(self, status: str | None = "pending") -> list[Record]:
        return self.store.list_approvals(status=status)

    def health(self) -> list[Record]:
        return self.env.health_overview()

    def verify_audit(self, incident_id: str) -> Record:
        if incident_id != "policy" and self.store.get_incident(incident_id) is None:
            raise KeyError(f"Unknown incident {incident_id}")
        return verify_incident(self.store, incident_id)

    def dead_letters(self, limit: int = 50) -> list[Record]:
        return self.store.list_dead_letters(limit)

    def policy(self) -> Record:
        policy = get_policy(self.store)
        return {**policy.doc, "version": policy.version}

    def test_policy(
        self,
        action: str,
        service: str,
        parameters: Record | None = None,
        confidence: float = 0.95,
        runbook_id: str | None = None,
    ) -> Record:
        """What the policy engine would decide for a proposal, without recording anything. Without
        `runbook_id`, assumes the proposal cites a runbook that recommends the action."""
        info = self.env.service_info(service)
        if runbook_id:
            runbook = runbooks.get(runbook_id)
            runbook_actions = runbook.actions if runbook else None
        else:
            runbook_id, runbook_actions = "(assumed)", (action,)
        proposal = RemediationProposal(
            incident_id="inc-0000000000",
            action=action,
            service=service,
            parameters=parameters or {},
            risk="low",
            rollback_plan="n/a",
            rationale="policy test",
            runbook_id=runbook_id,
        )
        facts = Facts(
            runbook_actions=runbook_actions,
            service_tier=int(info["tier"]),
            service_max_replicas=int(info["max_replicas"]),
            deployed_versions=tuple(d["version"] for d in self.env.deployments(service)),
            diagnosis_confidence=confidence,
            proposals_so_far=0,
        )
        return get_policy(self.store).evaluate(proposal, facts).model_dump()

    @staticmethod
    def search_runbooks(
        query: str, category: str | None = None, service: str | None = None, k: int = 3
    ) -> list[Record]:
        if not (query.strip() or category):
            return [rb.public() for rb in runbooks.all_runbooks().values()]
        return [rb.public(score) for rb, score in runbooks.search(query or category, k, category, service)]

    @staticmethod
    def contracts() -> Record:
        return {
            "lifecycle": {str(k): sorted(str(t) for t in v) for k, v in ALLOWED_TRANSITIONS.items()},
            "agents": [c.describe() for c in CONTRACTS.values()],
            "coordinator": {
                "tools": sorted(COORDINATOR_TOOLS),
                "transitions": sorted(f"{a} -> {b}" for a, b in COORDINATOR_TRANSITIONS),
            },
            "platform": {"transitions": sorted(f"{a} -> {b}" for a, b in PLATFORM_TRANSITIONS)},
        }
