"""Tools the agents call. Each factory binds tools to a store and environment.

Tools return JSON strings: easy for the model to read, and easy to parse in tests and in
the offline scripted agents.
"""

import json
from typing import Any

from strands import tool

from .. import approvals
from ..environment import Environment
from ..store import Store, now_iso

SEVERITIES = ("sev1", "sev2", "sev3", "sev4")


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


def _incident_view(store: Store, incident_id: str) -> dict:
    incident = store.get_incident(incident_id)
    if incident is None:
        return {"error": f"Unknown incident {incident_id}"}
    return {
        **incident,
        "approvals": [
            {k: a[k] for k in ("id", "action", "service", "params", "risk", "status", "decided_by", "note", "result")}
            for a in store.list_approvals(incident_id=incident_id)
        ],
    }


def common_tools(store: Store) -> list:
    @tool
    def get_incident(incident_id: str) -> str:
        """Get an incident: title, description, status, severity, affected service, findings so far,
        and every remediation approval with its status.

        Args:
            incident_id: The incident id, like "inc-1a2b3c4d5e".
        """
        return _json(_incident_view(store, incident_id))

    return [get_incident]


def observability_tools(env: Environment) -> list:
    @tool
    def get_health_overview() -> str:
        """Current health of every service: tier, healthy flag, error rate and p99 latency."""
        return _json(env.health_overview())

    @tool
    def get_service_info(service: str) -> str:
        """CMDB entry for a service: description, tier (1 = most critical), owner team,
        dependencies, current version, replica count and limit.

        Args:
            service: Service name, e.g. "checkout-api".
        """
        try:
            return _json(env.service_info(service))
        except KeyError as e:
            return _json({"error": str(e)})

    @tool
    def get_metrics(service: str) -> str:
        """Live metrics for a service: error rate, p99 latency, CPU, memory, request rate, restarts.

        Args:
            service: Service name.
        """
        try:
            return _json(env.metrics(service))
        except KeyError as e:
            return _json({"error": str(e)})

    return [get_health_overview, get_service_info, get_metrics]


def triage_tools(store: Store, env: Environment, agent: str) -> list:
    @tool
    def update_triage(incident_id: str, service: str, severity: str, summary: str) -> str:
        """Record the triage result on the incident.

        Args:
            incident_id: The incident id.
            service: The primary affected service (must exist in the CMDB).
            severity: One of sev1 (major customer impact), sev2, sev3, sev4 (minor).
            summary: One or two sentences: what is broken and who is affected.
        """
        if severity not in SEVERITIES:
            return _json({"error": f"severity must be one of {SEVERITIES}"})
        try:
            env.service_info(service)
        except KeyError as e:
            return _json({"error": str(e)})
        store.update_incident(
            incident_id, service=service, severity=severity, triage_summary=summary, status="investigating"
        )
        store.record(incident_id, agent, "triage", f"{severity.upper()} on {service}: {summary}")
        return _json({"ok": True})

    return [*common_tools(store), *observability_tools(env), update_triage]


def diagnostics_tools(store: Store, env: Environment, agent: str) -> list:
    @tool
    def search_logs(service: str, query: str = "") -> str:
        """Recent log lines for a service, optionally filtered by a case-insensitive substring.

        Args:
            service: Service name.
            query: Optional text to filter on, e.g. "ERROR" or "timeout".
        """
        try:
            return _json(env.logs(service, query))
        except KeyError as e:
            return _json({"error": str(e)})

    @tool
    def get_recent_deployments(service: str) -> str:
        """The last few deployments of a service, oldest first, with version and timestamp.

        Args:
            service: Service name.
        """
        try:
            return _json({"now": now_iso(), "deployments": env.deployments(service)})
        except KeyError as e:
            return _json({"error": str(e)})

    @tool
    def record_diagnosis(
        incident_id: str, root_cause: str, category: str, evidence: list[str], recommended_action: str
    ) -> str:
        """Record the root-cause diagnosis on the incident.

        Args:
            incident_id: The incident id.
            root_cause: One or two sentences naming the most likely root cause.
            category: One of "bad-deploy", "memory-leak", "saturation", "dependency", "unknown".
            evidence: Specific observations that support the diagnosis (metric values, log lines, deploy times).
            recommended_action: The remediation you recommend, in plain words.
        """
        store.update_incident(
            incident_id,
            root_cause=root_cause,
            category=category,
            evidence=evidence,
            recommended_action=recommended_action,
        )
        store.record(incident_id, agent, "diagnosis", root_cause, {"category": category, "evidence": evidence})
        return _json({"ok": True})

    return [*common_tools(store), *observability_tools(env), search_logs, get_recent_deployments, record_diagnosis]


def remediation_tools(store: Store, env: Environment, agent: str) -> list:
    @tool
    def get_runbook(topic: str) -> str:
        """Find the runbook for a failure mode, e.g. "bad-deploy", "memory-leak", "saturation".

        Args:
            topic: Failure mode or keywords.
        """
        return env.runbook(topic)

    @tool
    def propose_action(incident_id: str, action: str, service: str, rationale: str, replicas: int = 0) -> str:
        """Propose a remediation action. It is NOT executed now: it goes to a human for approval,
        and the platform executes it only after approval. Propose one action at a time.

        Args:
            incident_id: The incident id.
            action: One of rollback_deployment, restart_service, scale_service, flush_cache.
            service: The service to act on.
            rationale: Why this action, citing the diagnosis and runbook.
            replicas: For scale_service only: the new replica count.
        """
        params = {"replicas": replicas} if action == "scale_service" else {}
        try:
            approval = approvals.propose(
                store,
                env,
                incident_id=incident_id,
                agent=agent,
                action=action,
                service=service,
                params=params,
                rationale=rationale,
            )
        except (approvals.ApprovalError, KeyError) as e:
            return _json({"error": str(e)})
        return _json({"approval_id": approval["id"], "status": approval["status"], "risk": approval["risk"]})

    @tool
    def mark_mitigated(incident_id: str, verification: str) -> str:
        """Mark the incident mitigated. Only call this after get_metrics shows the service healthy.

        Args:
            incident_id: The incident id.
            verification: The metric values that show recovery.
        """
        incident = store.get_incident(incident_id)
        if incident is None:
            return _json({"error": f"Unknown incident {incident_id}"})
        if incident.get("service") and not env.metrics(incident["service"])["healthy"]:
            return _json({"error": f"{incident['service']} is still unhealthy; do not mark mitigated"})
        store.update_incident(incident_id, status="mitigated", mitigated_at=now_iso(), verification=verification)
        store.record(incident_id, agent, "mitigated", verification)
        return _json({"ok": True})

    return [*common_tools(store), *observability_tools(env), get_runbook, propose_action, mark_mitigated]


def communications_tools(store: Store, agent: str) -> list:
    @tool
    def get_incident_timeline(incident_id: str) -> str:
        """The incident's timeline: agent findings, delegations, human decisions and actions, oldest first.

        Args:
            incident_id: The incident id.
        """
        events = [e for e in store.list_events(incident_id) if e["kind"] != "tool.call"]
        return _json([{k: e[k] for k in ("created_at", "actor", "kind", "message")} for e in events])

    @tool
    def post_status_update(incident_id: str, audience: str, message: str) -> str:
        """Post a status update to stakeholders.

        Args:
            incident_id: The incident id.
            audience: "internal" (engineering and support) or "customers" (status page).
            message: The update. Plain language, no internal jargon for customers.
        """
        if audience not in ("internal", "customers"):
            return _json({"error": "audience must be 'internal' or 'customers'"})
        store.record(incident_id, agent, f"status_update.{audience}", message)
        return _json({"ok": True})

    @tool
    def resolve_incident(incident_id: str, postmortem: str) -> str:
        """Close a mitigated incident with a blameless postmortem.

        Args:
            incident_id: The incident id.
            postmortem: Markdown with Summary, Impact, Timeline, Root cause, Resolution, Follow-ups.
        """
        incident = store.get_incident(incident_id)
        if incident is None:
            return _json({"error": f"Unknown incident {incident_id}"})
        if incident["status"] != "mitigated":
            return _json({"error": f"Incident is {incident['status']}; only a mitigated incident can be resolved"})
        store.update_incident(incident_id, status="resolved", resolved_at=now_iso(), postmortem=postmortem)
        store.record(incident_id, agent, "resolved", "Incident resolved; postmortem written")
        return _json({"ok": True})

    return [*common_tools(store), get_incident_timeline, post_status_update, resolve_incident]


def coordinator_tools(store: Store, agent: str) -> list:
    @tool
    def escalate_incident(incident_id: str, reason: str) -> str:
        """Hand the incident to the owning team's on-call engineer when the agents cannot resolve it.

        Args:
            incident_id: The incident id.
            reason: What was tried and why it needs a human.
        """
        if store.get_incident(incident_id) is None:
            return _json({"error": f"Unknown incident {incident_id}"})
        store.update_incident(incident_id, status="escalated", escalation_reason=reason)
        store.record(incident_id, agent, "escalated", reason)
        return _json({"ok": True})

    return [*common_tools(store), escalate_incident]
