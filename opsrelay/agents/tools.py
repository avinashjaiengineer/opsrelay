"""Tools the agents call. Each factory binds tools to a store and environment.

Tools that submit an agent's result validate it against its pydantic model (opsrelay.schemas),
check the agent's contract (opsrelay.contracts) and change state only through the lifecycle
(opsrelay.lifecycle). Invalid or out-of-contract calls return {"error": ...} with the reasons,
so the agent can correct itself; nothing invalid reaches the workflow.

Tools return JSON strings: easy for the model to read, and easy to parse in tests and in
the offline scripted agents.
"""

import json
from typing import Any, Literal

from pydantic import ValidationError
from strands import tool

from .. import approvals, memory, runbooks
from ..contracts import CONTRACTS
from ..environment import Environment
from ..lifecycle import IllegalTransition, Status, status_of, transition
from ..policy import get_policy
from ..schemas import (
    DiagnosisResult,
    Postmortem,
    RemediationDecline,
    RemediationProposal,
    StatusUpdate,
    TriageResult,
    VerificationResult,
    validation_errors,
)
from ..store import Record, Store, now_iso
from ..store.base import make_event
from .meta import agent_meta

AUDIT_NOISE = ("tool.invoked", "tool.completed", "tool.failed", "status.changed")


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


def _error(message: str, **extra: Any) -> str:
    return _json({"error": message, **extra})


def _invalid(model: str, e: ValidationError) -> str:
    return _error(f"{model} is invalid; fix these fields and submit again", problems=validation_errors(e))


def _event(role: str, incident_id: str, kind: str, message: str, result: Record) -> Record:
    """An agent's typed result as an audit event, carrying the agent, model and prompt versions."""
    return make_event(incident_id, role, kind, message, {"result": result, **agent_meta(role)}, output=result)


def _emit(store: Store, role: str, incident_id: str, kind: str, message: str, result: Record) -> None:
    """For results that don't change state; state changes carry their event in the same commit."""
    store.append_event(_event(role, incident_id, kind, message, result))


def _acting(store: Store, incident_id: str, role: str) -> tuple[Record | None, str | None]:
    """The incident, if this role's contract lets it act on it now; else an error for the agent."""
    incident = store.get_incident(incident_id)
    if incident is None:
        return None, _error(f"Unknown incident {incident_id}")
    problem = CONTRACTS[role].check_dispatch(status_of(incident), incident)
    return (None, _error(problem)) if problem else (incident, None)


def _incident_view(store: Store, incident_id: str) -> dict:
    incident = store.get_incident(incident_id)
    if incident is None:
        return {"error": f"Unknown incident {incident_id}"}
    keys = (
        "id",
        "action",
        "service",
        "params",
        "runbook_id",
        "risk",
        "status",
        "decided_by",
        "note",
        "result",
        "policy",
    )
    return {
        **incident,
        "approvals": [{k: a.get(k) for k in keys} for a in store.list_approvals(incident_id=incident_id)],
    }


def common_tools(store: Store) -> list:
    @tool
    def get_incident(incident_id: str) -> str:
        """Get an incident: title, description, status, severity, affected service, the typed results
        of triage, diagnosis and verification so far, and every remediation approval with its policy
        decision and status.

        Args:
            incident_id: The incident id, like "inc-1a2b3c4d5e".
        """
        return _json(_incident_view(store, incident_id))

    return [get_incident]


def observability_tools(env: Environment) -> dict[str, Any]:
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
            return _error(str(e))

    @tool
    def get_metrics(service: str) -> str:
        """Live metrics for a service: error rate, p99 latency, CPU, memory, request rate, restarts.

        Args:
            service: Service name.
        """
        try:
            return _json(env.metrics(service))
        except KeyError as e:
            return _error(str(e))

    return {t.tool_name: t for t in (get_health_overview, get_service_info, get_metrics)}


def triage_tools(store: Store, env: Environment, role: str = "triage") -> list:
    @tool
    def submit_triage(
        incident_id: str,
        severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"],
        service: str,
        customer_impact: str,
        rationale: str,
        confidence: float,
    ) -> str:
        """Submit your triage result (a TriageResult). This moves the incident from triaging to
        investigating. Submit exactly once.

        Args:
            incident_id: The incident id.
            severity: SEV1 (tier-1 service down or most users affected), SEV2 (tier-1 degraded),
                SEV3 (tier-2 degraded) or SEV4 (minor).
            service: The primary affected service (must exist in the CMDB).
            customer_impact: What customers experience, in one or two sentences.
            rationale: Why this service and severity, citing the health data you looked at.
            confidence: How sure you are, from 0.0 to 1.0.
        """
        try:
            result = TriageResult(
                incident_id=incident_id,
                severity=severity,
                service=service,
                customer_impact=customer_impact,
                rationale=rationale,
                confidence=confidence,
            )
        except ValidationError as e:
            return _invalid("TriageResult", e)
        _incident, err = _acting(store, result.incident_id, role)
        if err:
            return err
        try:
            env.service_info(result.service)
        except KeyError as e:
            return _error(str(e).strip("'\""))
        dump = result.model_dump()
        try:
            transition(
                store,
                result.incident_id,
                Status.INVESTIGATING,
                actor=role,
                reason=f"{result.severity} on {result.service}",
                service=result.service,
                severity=result.severity,
                customer_impact=result.customer_impact,
                triage=dump,
                events=[
                    _event(
                        role,
                        result.incident_id,
                        "incident.triaged",
                        f"{result.severity} on {result.service}: {result.customer_impact}",
                        dump,
                    )
                ],
            )
        except IllegalTransition as e:
            return _error(str(e))
        return _json({"ok": True, "status": "investigating"})

    @tool
    def report_inconclusive_triage(incident_id: str, reason: str) -> str:
        """Report that you cannot identify an affected service (for example, everything is healthy).
        The incident then goes to a human. Use this instead of guessing.

        Args:
            incident_id: The incident id.
            reason: What you checked and why no service can be identified.
        """
        _incident, err = _acting(store, incident_id, role)
        if err:
            return err
        if not reason.strip():
            return _error("reason is required")
        _emit(store, role, incident_id, "triage.inconclusive", reason.strip(), {"reason": reason.strip()})
        return _json({"ok": True})

    observe = observability_tools(env)
    return [*common_tools(store), *observe.values(), submit_triage, report_inconclusive_triage]


def knowledge_tools(store: Store) -> dict[str, Any]:
    @tool
    def search_runbooks(query: str, category: str = "", service: str = "") -> str:
        """Search the runbooks: the team's procedures for each kind of incident. Returns the best
        matches, each with its id, the actions it recommends and its steps. Cite the id you follow.

        Args:
            query: What is wrong, in words, e.g. "pods OOMKilled after deploy, login latency high".
            category: Optional failure mode (bad-deploy, memory-leak, saturation, dependency, unknown);
                runbooks for it rank higher.
            service: Optional affected service; runbooks written for it rank higher.
        """
        hits = runbooks.search(query or category, k=3, category=category or None, service=service or None)
        return _json([rb.public(score) for rb, score in hits])

    @tool
    def find_similar_incidents(incident_id: str) -> str:
        """Past incidents most like this one (incident memory): their root cause, the action taken and
        its outcome, proposals people rejected and why, and the lessons from the postmortem. Use them
        as hints, not proof: confirm with this incident's own evidence.

        Args:
            incident_id: The incident id.
        """
        incident = store.get_incident(incident_id)
        if incident is None:
            return _error(f"Unknown incident {incident_id}")
        found = memory.similar(store, incident)
        return _json(found or {"similar": [], "note": "No similar past incidents."})

    return {"search_runbooks": search_runbooks, "find_similar_incidents": find_similar_incidents}


def diagnostics_tools(store: Store, env: Environment, role: str = "diagnostics") -> list:
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
            return _error(str(e))

    @tool
    def get_recent_deployments(service: str) -> str:
        """The last few deployments of a service, oldest first, with version and timestamp.

        Args:
            service: Service name.
        """
        try:
            return _json({"now": now_iso(), "deployments": env.deployments(service)})
        except KeyError as e:
            return _error(str(e))

    @tool
    def submit_diagnosis(
        incident_id: str,
        root_cause: str,
        category: Literal["bad-deploy", "memory-leak", "saturation", "dependency", "unknown"],
        evidence: list[dict[str, str]],
        confidence: float,
        affected_component: str,
        recommended_action: str,
    ) -> str:
        """Submit your root-cause diagnosis (a DiagnosisResult). Submit exactly once.

        Args:
            incident_id: The incident id.
            root_cause: One or two sentences naming the most likely root cause.
            category: The failure mode; use "unknown" if the evidence doesn't support a conclusion.
            evidence: Observations that support the diagnosis, each {"source": ..., "value": ...}
                where source is one of metrics, logs, deployment, dependency, cmdb, alert, runbook,
                history (a similar past incident), and value
                is the specific observation (a metric value, log line or deploy time).
            confidence: How sure you are, from 0.0 to 1.0. Below 0.7 no action will be allowed and
                the incident goes to a human; be honest.
            affected_component: The specific component at fault, e.g. "PriceCalculator.applyPromotion".
            recommended_action: The remediation you recommend, in plain words.
        """
        try:
            result = DiagnosisResult(
                incident_id=incident_id,
                root_cause=root_cause,
                category=category,
                evidence=evidence,
                confidence=confidence,
                affected_component=affected_component,
                recommended_action=recommended_action,
            )
        except ValidationError as e:
            return _invalid("DiagnosisResult", e)
        _incident, err = _acting(store, result.incident_id, role)
        if err:
            return err
        dump = result.model_dump()
        store.update_incident(
            result.incident_id,
            diagnosis=dump,
            root_cause=result.root_cause,
            category=result.category,
            recommended_action=result.recommended_action,
            events=[
                _event(
                    role,
                    result.incident_id,
                    "diagnosis.completed",
                    f"{result.root_cause} (confidence {result.confidence:.0%})",
                    dump,
                )
            ],
        )
        return _json({"ok": True})

    observe = observability_tools(env)
    return [
        *common_tools(store),
        *observe.values(),
        search_logs,
        get_recent_deployments,
        *knowledge_tools(store).values(),
        submit_diagnosis,
    ]


NO_ACTION = frozenset({"", "none", "no_action", "no-action", "noop", "no-op", "null", "n/a"})


def _deployed_spelling(env: Environment, service: str, version: str) -> str:
    """ "v2.13.4" -> "2.13.4" (or back) when that is how the service's deployments name it."""
    try:
        deployed = [d["version"] for d in env.deployments(service)]
    except KeyError:
        return version
    if version in deployed:
        return version
    bare = version.strip().lstrip("vV")
    return next((d for d in deployed if d.lstrip("vV") == bare), version)


def remediation_tools(store: Store, env: Environment, role: str = "remediation") -> list:
    @tool
    def list_allowed_actions() -> str:
        """The actions the policy engine knows: base risk, whether a person must approve, the
        parameters each takes, and actions that are never allowed."""
        return _json(get_policy(store).actions)

    @tool
    def submit_proposal(
        incident_id: str,
        action: str,
        service: str,
        risk: Literal["low", "medium", "high", "critical"],
        rollback_plan: str,
        rationale: str,
        runbook_id: str = "",
        replicas: int = 0,
        target_version: str = "",
    ) -> str:
        """Propose one remediation action (a RemediationProposal). It is NOT executed by you: the
        policy engine evaluates it, a person approves it if required, and only then does the
        platform run it. Propose one action at a time.

        Args:
            incident_id: The incident id.
            action: One of the actions from list_allowed_actions, e.g. rollback_deployment.
            service: The service to act on.
            risk: Your own assessment of the risk. The policy may raise it, never lower it.
            rollback_plan: How to undo this action if it makes things worse.
            rationale: Why this action, citing the diagnosis and the runbook.
            runbook_id: The id of the runbook you follow, from search_runbooks (e.g. "RB-002"). An
                uncited action, or one the runbook doesn't recommend, needs a person's approval.
            replicas: For scale_service only: the new replica count.
            target_version: For rollback_deployment only (optional): the version to roll back to.
        """
        if action.strip().lower() in NO_ACTION:
            return _error("That is not an action. If no action fits, call decline_remediation instead.")
        parameters: dict[str, int | str] = {}
        if replicas:
            parameters["replicas"] = replicas
        if target_version:
            parameters["target_version"] = _deployed_spelling(env, service, target_version)
        try:
            proposal = RemediationProposal(
                incident_id=incident_id,
                action=action,
                service=service,
                parameters=parameters,
                risk=risk,
                rollback_plan=rollback_plan,
                rationale=rationale,
                runbook_id=runbook_id or None,
            )
        except ValidationError as e:
            return _invalid("RemediationProposal", e)
        _incident, err = _acting(store, proposal.incident_id, role)
        if err:
            return err
        try:
            approval = approvals.propose(store, env, proposal, agent=role, meta=agent_meta(role))
        except approvals.PolicyDenied as e:
            return _error(
                "Denied by policy",
                reasons=e.decision.reasons,
                next_step="Propose a different action, or call decline_remediation if nothing safe applies.",
            )
        except (approvals.ApprovalError, IllegalTransition) as e:
            return _error(str(e))
        return _json(
            {
                "approval_id": approval["id"],
                "status": approval["status"],
                "risk": approval["risk"],
                "decision": approval["policy"]["decision"],
                "reasons": approval["policy"]["reasons"],
            }
        )

    @tool
    def decline_remediation(incident_id: str, reason: str) -> str:
        """Decline to propose an action because no safe runbook action fits (a RemediationDecline).
        The incident then goes to a human.

        Args:
            incident_id: The incident id.
            reason: Why no action is safe or appropriate.
        """
        try:
            result = RemediationDecline(incident_id=incident_id, reason=reason)
        except ValidationError as e:
            return _invalid("RemediationDecline", e)
        _incident, err = _acting(store, result.incident_id, role)
        if err:
            return err
        _emit(store, role, result.incident_id, "remediation.declined", result.reason, result.model_dump())
        return _json({"ok": True})

    observe = observability_tools(env)
    return [
        *common_tools(store),
        observe["get_service_info"],
        observe["get_metrics"],
        *knowledge_tools(store).values(),
        list_allowed_actions,
        submit_proposal,
        decline_remediation,
    ]


def verification_tools(store: Store, env: Environment, role: str = "verification") -> list:
    @tool
    def submit_verification(
        incident_id: str,
        service: str,
        recovered: bool,
        observations: list[dict[str, str]],
        summary: str,
    ) -> str:
        """Submit whether the service recovered after the remediation (a VerificationResult).
        The platform checks your conclusion against live metrics. If the service did not
        recover, the incident moves to failed.

        Args:
            incident_id: The incident id.
            service: The incident's affected service.
            recovered: True only if metrics show the service healthy again.
            observations: The measurements you based this on, each {"source": "metrics", "value": ...}.
            summary: One or two sentences on the outcome.
        """
        try:
            result = VerificationResult(
                incident_id=incident_id,
                service=service,
                recovered=recovered,
                observations=observations,
                summary=summary,
            )
        except ValidationError as e:
            return _invalid("VerificationResult", e)
        incident, err = _acting(store, result.incident_id, role)
        if err:
            return err
        if incident.get("service") and result.service != incident["service"]:
            return _error(f"This incident's service is {incident['service']}, not {result.service}")
        try:
            healthy = env.metrics(result.service)["healthy"]
        except KeyError as e:
            return _error(str(e))
        if healthy != result.recovered:
            return _error(
                f"Your conclusion recovered={result.recovered} contradicts live metrics (healthy={healthy}). "
                "Check get_metrics again."
            )
        dump = result.model_dump()
        event = _event(
            role,
            result.incident_id,
            "verification.completed",
            ("Recovered: " if result.recovered else "Not recovered: ") + result.summary,
            dump,
        )
        if result.recovered:
            store.update_incident(result.incident_id, verification=dump, verified_at=now_iso(), events=[event])
            return _json({"ok": True, "status": "verifying", "recovered": True})
        try:
            transition(
                store,
                result.incident_id,
                Status.FAILED,
                actor=role,
                reason=result.summary,
                verification=dump,
                failure_reason=result.summary,
                events=[event],
            )
        except IllegalTransition as e:
            return _error(str(e))
        return _json({"ok": True, "status": "failed", "recovered": False})

    observe = observability_tools(env)
    return [*common_tools(store), observe["get_health_overview"], observe["get_metrics"], submit_verification]


def communications_tools(store: Store, role: str = "communications") -> list:
    @tool
    def get_incident_timeline(incident_id: str) -> str:
        """The incident's timeline: agent findings, delegations, policy decisions, human decisions
        and actions, oldest first.

        Args:
            incident_id: The incident id.
        """
        events = [e for e in store.list_events(incident_id) if e["kind"] not in AUDIT_NOISE]
        return _json([{k: e[k] for k in ("created_at", "actor", "kind", "message")} for e in events])

    @tool
    def post_status_update(incident_id: str, audience: Literal["internal", "customers"], message: str) -> str:
        """Post a status update (a StatusUpdate).

        Args:
            incident_id: The incident id.
            audience: "internal" (engineering and support) or "customers" (status page: plain
                language, no internal detail).
            message: The update.
        """
        try:
            update = StatusUpdate(incident_id=incident_id, audience=audience, message=message)
        except ValidationError as e:
            return _invalid("StatusUpdate", e)
        _incident, err = _acting(store, update.incident_id, role)
        if err:
            return err
        _emit(store, role, update.incident_id, f"status_update.{update.audience}", update.message, update.model_dump())
        return _json({"ok": True})

    @tool
    def submit_postmortem(
        incident_id: str,
        summary: str,
        impact: str,
        timeline: list[str],
        root_cause: str,
        resolution: str,
        detection: str,
        action_items: list[str],
        contributing_factors: list[str] | None = None,
    ) -> str:
        """Submit the blameless postmortem (a Postmortem) and resolve the incident. Only possible
        once verification has confirmed the service recovered.

        Args:
            incident_id: The incident id.
            summary: What happened, in two or three sentences.
            impact: Who was affected, how, and for how long.
            timeline: Key moments, oldest first, each "HH:MM:SS what happened".
            root_cause: The root cause from the diagnosis.
            resolution: What fixed it and how recovery was verified.
            detection: How the incident was detected, and how it could be detected sooner.
            action_items: Concrete follow-ups that would prevent a repeat.
            contributing_factors: Anything that made it worse or slower to fix.
        """
        try:
            postmortem = Postmortem(
                incident_id=incident_id,
                summary=summary,
                impact=impact,
                timeline=timeline,
                root_cause=root_cause,
                resolution=resolution,
                detection=detection,
                action_items=action_items,
                contributing_factors=contributing_factors or [],
            )
        except ValidationError as e:
            return _invalid("Postmortem", e)
        incident, err = _acting(store, postmortem.incident_id, role)
        if err:
            return err
        if incident["status"] != Status.VERIFYING or not (incident.get("verification") or {}).get("recovered"):
            return _error("Only an incident whose recovery has been verified can be resolved")
        posted = {e["kind"] for e in store.list_events(postmortem.incident_id)}
        missing = [a for a in ("internal", "customers") if f"status_update.{a}" not in posted]
        if missing:
            return _error(
                "Stakeholders must be updated before the incident is resolved: post a status update for "
                + " and ".join(missing)
                + " first"
            )
        dump = postmortem.model_dump()
        try:
            transition(
                store,
                postmortem.incident_id,
                Status.RESOLVED,
                actor=role,
                reason="postmortem written",
                postmortem=postmortem.markdown(incident["title"]),
                postmortem_data=dump,
                resolved_at=now_iso(),
                events=[
                    _event(
                        role, postmortem.incident_id, "incident.resolved", "Incident resolved; postmortem written", dump
                    )
                ],
            )
        except IllegalTransition as e:
            return _error(str(e))
        return _json({"ok": True, "status": "resolved"})

    return [*common_tools(store), get_incident_timeline, post_status_update, submit_postmortem]


def coordinator_tools(store: Store, role: str = "coordinator") -> list:
    @tool
    def escalate_incident(incident_id: str, reason: str) -> str:
        """Hand the incident to the owning team's on-call engineer when the agents cannot resolve it.

        Args:
            incident_id: The incident id.
            reason: What was tried and why it needs a human.
        """
        if not reason.strip():
            return _error("reason is required")
        incident = store.get_incident(incident_id)
        if incident and incident["status"] == Status.AWAITING_APPROVAL:
            return _error(
                "Don't escalate: the incident is waiting for a person to approve or reject the proposal, "
                "which is the human step. Stop here and report that it awaits approval."
            )
        try:
            transition(
                store,
                incident_id,
                Status.ESCALATED,
                actor=role,
                reason=reason,
                escalation_reason=reason,
                events=[_event(role, incident_id, "incident.escalated", reason, {"reason": reason})],
            )
        except (IllegalTransition, KeyError) as e:
            return _error(str(e).strip("'\""))
        return _json({"ok": True, "status": "escalated"})

    return [*common_tools(store), escalate_incident]


TOOL_FACTORIES = {
    "triage": triage_tools,
    "diagnostics": diagnostics_tools,
    "remediation": remediation_tools,
    "verification": verification_tools,
}
