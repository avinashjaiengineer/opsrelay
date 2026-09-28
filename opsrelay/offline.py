"""Offline mode: a scripted Strands model provider.

`ScriptedModel` implements the Strands `Model` interface, so offline agents are the very same
`strands.Agent` objects, with the same tools, hooks and A2A wiring, as the Bedrock-backed
agents. Only the decisions come from a deterministic playbook instead of an LLM. That keeps
the full agent-to-agent workflow runnable in CI and in demos without an AWS account.
"""

import json
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from strands.models.model import Model

INCIDENT_RE = re.compile(r"\binc-[0-9a-f]{10}\b")


@dataclass
class Call:
    name: str
    input: dict[str, Any]


@dataclass
class Script:
    """What a policy sees: the task prompt and the tool calls made so far with their results."""

    prompt: str
    history: list[tuple[str, dict[str, Any], Any]] = field(default_factory=list)

    def result(self, name: str) -> Any:
        """Result of the most recent call to `name`, or None if it has not been called."""
        for tool_name, _input, result in reversed(self.history):
            if tool_name == name:
                return result
        return None

    def called(self, name: str) -> bool:
        return any(tool_name == name for tool_name, _i, _r in self.history)

    @property
    def incident_id(self) -> str:
        match = INCIDENT_RE.search(self.prompt)
        return match.group(0) if match else ""


Policy = Callable[[Script], "Call | str"]


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _build_script(messages: list[dict[str, Any]]) -> Script:
    prompt = ""
    for message in reversed(messages):
        if message["role"] == "user":
            texts = [b["text"] for b in message["content"] if "text" in b]
            if texts:
                prompt = "\n".join(texts)
                break
    script = Script(prompt=prompt)
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for message in messages:
        for block in message["content"]:
            if "toolUse" in block:
                use = block["toolUse"]
                pending[use["toolUseId"]] = (use["name"], use.get("input") or {})
            elif "toolResult" in block:
                res = block["toolResult"]
                name, inp = pending.pop(res["toolUseId"], ("?", {}))
                text = "".join(c.get("text", "") for c in res.get("content", []))
                script.history.append((name, inp, _parse(text)))
    return script


class ScriptedModel(Model):
    def __init__(self, policy: Policy, name: str = "scripted"):
        self.policy = policy
        self.config = {"model_id": f"offline:{name}"}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> Any:
        return self.config

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # noqa: ANN001
        raise NotImplementedError("ScriptedModel does not support structured output")

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        decision = self.policy(_build_script(messages))
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(decision, Call):
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": f"tooluse_{uuid.uuid4().hex[:12]}", "name": decision.name}}
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(decision.input)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": decision}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": 0},
            }
        }


# --- Playbooks -------------------------------------------------------------------------------


def _submitted(s: Script, name: str) -> bool:
    """True if `name` was called and did not return an error."""
    result = s.result(name)
    return s.called(name) and not (isinstance(result, dict) and "error" in result)


def triage_policy(s: Script) -> Call | str:
    iid = s.incident_id
    if not iid:
        return "No incident id in the request."
    incident = s.result("get_incident")
    if incident is None:
        return Call("get_incident", {"incident_id": iid})
    health = s.result("get_health_overview")
    if health is None:
        return Call("get_health_overview", {})
    if _submitted(s, "submit_triage"):
        _name, done, _result = next(h for h in reversed(s.history) if h[0] == "submit_triage")
        return f"Triage complete for {iid}: {done['severity']} on {done['service']}. {done['customer_impact']}"
    if s.called("report_inconclusive_triage"):
        return f"Triage inconclusive for {iid}: every service reports healthy."

    by_name = {h["service"]: h for h in health}
    text = f"{incident.get('title', '')} {incident.get('description', '')}".lower()
    service = incident.get("service") or next((n for n in by_name if n in text), None)
    if service is None:
        unhealthy = sorted((h for h in health if not h["healthy"]), key=lambda h: h["tier"])
        service = unhealthy[0]["service"] if unhealthy else None
    if service is None:
        return Call(
            "report_inconclusive_triage",
            {"incident_id": iid, "reason": "Every service reports healthy; no affected service can be identified."},
        )
    h = by_name[service]
    severity = "SEV1" if h["tier"] == 1 and h["error_rate"] >= 0.1 else "SEV2" if h["tier"] == 1 else "SEV3"
    return Call(
        "submit_triage",
        {
            "incident_id": iid,
            "severity": severity,
            "service": service,
            "customer_impact": (
                f"{service} is degraded: error rate {h['error_rate']:.1%}, p99 {h['p99_latency_ms']} ms."
            ),
            "rationale": f"{service} is tier {h['tier']} and the only unhealthy service named by the alert.",
            "confidence": 0.95,
        },
    )


def _recent(ts: str, hours: float = 2) -> bool:
    try:
        return datetime.now(UTC) - datetime.fromisoformat(ts) < timedelta(hours=hours)
    except ValueError:
        return False


def diagnostics_policy(s: Script) -> Call | str:
    iid = s.incident_id
    incident = s.result("get_incident")
    if incident is None:
        return Call("get_incident", {"incident_id": iid})
    service = incident.get("service")
    if not service:
        return f"{iid} has no affected service yet; run triage first."
    metrics = s.result("get_metrics")
    if metrics is None:
        return Call("get_metrics", {"service": service})
    logs = s.result("search_logs")
    if logs is None:
        return Call("search_logs", {"service": service, "query": ""})
    deploys = s.result("get_recent_deployments")
    if deploys is None:
        return Call("get_recent_deployments", {"service": service})
    if _submitted(s, "submit_diagnosis"):
        return f"Diagnosis recorded for {iid}."

    latest = deploys["deployments"][-1] if deploys.get("deployments") else None
    evidence = [
        {"source": "metrics", "value": f"error_rate={metrics['error_rate']}, p99={metrics['p99_latency_ms']} ms"},
        {"source": "metrics", "value": f"cpu={metrics['cpu_pct']}%, memory={metrics['memory_pct']}%"},
    ]
    if metrics["error_rate"] >= 0.1 and latest and _recent(latest["at"]):
        category, confidence = "bad-deploy", 0.95
        root = f"Release {latest['version']} of {service}, deployed at {latest['at']}, introduced an exception."
        component = f"{service} {latest['version']}"
        action = "Roll back to the previous version."
        evidence.append({"source": "deployment", "value": f"{latest['version']} deployed at {latest['at']}"})
    elif metrics["memory_pct"] >= 90:
        category, confidence = "memory-leak", 0.92
        root = f"{service} is exhausting memory and pods are being OOMKilled."
        component = f"{service} memory"
        action = "Rolling restart to reclaim memory; investigate the leak."
    elif metrics["cpu_pct"] >= 85:
        category, confidence = "saturation", 0.93
        root = f"{service} is CPU-saturated by traffic at {metrics['requests_per_min']} req/min."
        component = f"{service} capacity"
        action = "Scale out replicas."
    else:
        category, confidence = "unknown", 0.3
        root = f"No clear cause found for {service}."
        component = service
        action = "Escalate to the owning team."
    evidence.extend({"source": "logs", "value": line} for line in (logs[:2] if isinstance(logs, list) else []))
    return Call(
        "submit_diagnosis",
        {
            "incident_id": iid,
            "root_cause": root,
            "category": category,
            "evidence": evidence,
            "confidence": confidence,
            "affected_component": component,
            "recommended_action": action,
        },
    )


# The action this playbook prefers for each failure mode, if the runbook recommends it.
PREFERRED_ACTION = {
    "bad-deploy": "rollback_deployment",
    "memory-leak": "restart_service",
    "saturation": "scale_service",
}
ACTION_PLAN = {
    "rollback_deployment": ("high", "Redeploy the release that was rolled back."),
    "restart_service": ("medium", "None needed: a restart does not change the deployed version."),
    "scale_service": ("low", "Scale back to the previous replica count."),
    "flush_cache": ("low", "None needed: the cache refills from the source of truth."),
}


def remediation_policy(s: Script) -> Call | str:
    iid = s.incident_id
    incident = s.result("get_incident")
    if incident is None:
        return Call("get_incident", {"incident_id": iid})
    service = incident.get("service")
    if incident["status"] != "investigating":
        return f"{iid} is {incident['status']}; nothing to propose."
    if s.called("decline_remediation"):
        return f"Declined to remediate {iid}."
    proposed = s.result("submit_proposal")
    if proposed is not None:
        if "error" in proposed:
            return Call(
                "decline_remediation",
                {"incident_id": iid, "reason": f"{proposed['error']}: {'; '.join(proposed.get('reasons', []))}"},
            )
        return (
            f"Proposed remediation for {iid}: {proposed['decision']} ({proposed['risk']} risk), {proposed['status']}."
        )

    category = incident.get("category", "unknown")
    hits = s.result("search_runbooks")
    if hits is None:
        query = f"{incident.get('title', '')}. {incident.get('root_cause', '')}"
        return Call("search_runbooks", {"query": query, "category": category, "service": service or ""})
    fitting = [h for h in hits if category in h["categories"]]
    preferred = PREFERRED_ACTION.get(category)
    runbook = next((h for h in fitting if preferred in h["recommended_actions"]), fitting[0] if fitting else None)
    actions = [a for a in (runbook or {}).get("recommended_actions", []) if a in ACTION_PLAN]
    if runbook is None or not actions:
        cited = f"Runbook {runbook['id']} recommends no automated action" if runbook else "No runbook fits"
        return Call("decline_remediation", {"incident_id": iid, "reason": f"{cited} for category '{category}'."})
    action = preferred if preferred in actions else actions[0]
    risk, rollback_plan = ACTION_PLAN[action]
    extra: dict[str, Any] = {}
    if action == "scale_service":
        info = s.result("get_service_info")
        if info is None:
            return Call("get_service_info", {"service": service})
        extra["replicas"] = min(info["max_replicas"], info["replicas"] * 2)
    return Call(
        "submit_proposal",
        {
            "incident_id": iid,
            "action": action,
            "service": service,
            "risk": risk,
            "rollback_plan": rollback_plan,
            "rationale": f"Runbook {runbook['id']} ({runbook['title']}): {incident.get('recommended_action', '')}",
            "runbook_id": runbook["id"],
            **extra,
        },
    )


def verification_policy(s: Script) -> Call | str:
    iid = s.incident_id
    incident = s.result("get_incident")
    if incident is None:
        return Call("get_incident", {"incident_id": iid})
    service = incident.get("service")
    metrics = s.result("get_metrics")
    if metrics is None:
        return Call("get_metrics", {"service": service})
    if _submitted(s, "submit_verification"):
        return f"Verified {service}: {'recovered' if metrics['healthy'] else 'not recovered'}."
    return Call(
        "submit_verification",
        {
            "incident_id": iid,
            "service": service,
            "recovered": metrics["healthy"],
            "observations": [
                {"source": "metrics", "value": f"error_rate={metrics['error_rate']}"},
                {"source": "metrics", "value": f"p99_latency_ms={metrics['p99_latency_ms']}"},
            ],
            "summary": f"{service} error rate {metrics['error_rate']}, p99 {metrics['p99_latency_ms']} ms: "
            + ("healthy." if metrics["healthy"] else "still unhealthy."),
        },
    )


FOLLOW_UPS = {
    "bad-deploy": ["Add a canary stage that checks the 5xx rate before full rollout", "Add a regression test"],
    "memory-leak": ["Find and fix the leak in the session cache", "Alert on memory above 85% for 10 minutes"],
    "saturation": ["Autoscale on CPU and request rate", "Load-test for campaign traffic"],
}


def communications_policy(s: Script) -> Call | str:
    iid = s.incident_id
    incident = s.result("get_incident")
    if incident is None:
        return Call("get_incident", {"incident_id": iid})
    status = incident["status"]
    posted = [inp["audience"] for name, inp, _r in s.history if name == "post_status_update"]
    verified = status == "verifying" and (incident.get("verification") or {}).get("recovered")

    if verified:
        timeline = s.result("get_incident_timeline")
        if timeline is None:
            return Call("get_incident_timeline", {"incident_id": iid})
        if "internal" not in posted:
            return Call(
                "post_status_update",
                {
                    "incident_id": iid,
                    "audience": "internal",
                    "message": f"[{iid}] Recovered. Cause: {incident.get('root_cause')} "
                    f"Fix: {incident.get('recommended_action')}",
                },
            )
        if "customers" not in posted:
            return Call(
                "post_status_update",
                {
                    "incident_id": iid,
                    "audience": "customers",
                    "message": "The issue affecting our service has been fixed and everything is working normally. "
                    "We're sorry for the disruption.",
                },
            )
        if not _submitted(s, "submit_postmortem"):
            executed = next((a for a in incident["approvals"] if a["status"] == "executed"), {})
            return Call(
                "submit_postmortem",
                {
                    "incident_id": iid,
                    "summary": incident.get("customer_impact") or incident["title"],
                    "impact": f"{incident.get('severity')} on {incident.get('service')}: {incident.get('title')}.",
                    "timeline": [f"{e['created_at'][11:19]} {e['actor']}: {e['message'][:200]}" for e in timeline][:15],
                    "root_cause": incident.get("root_cause") or "Unknown",
                    "resolution": f"{(executed.get('result') or {}).get('detail', 'Remediation ran')}. "
                    f"Verified: {incident['verification']['summary']}",
                    "detection": f"Detected by the {incident.get('source', 'monitoring')} alert: {incident['title']}.",
                    "action_items": FOLLOW_UPS.get(
                        incident.get("category", ""), ["Review monitoring for this failure"]
                    ),
                },
            )
        return f"Stakeholders updated and incident {iid} resolved with a postmortem."

    if "internal" not in posted:
        detail = incident.get("escalation_reason") or incident.get("customer_impact") or incident["title"]
        return Call(
            "post_status_update",
            {"incident_id": iid, "audience": "internal", "message": f"[{iid}] Status: {status}. {detail}"},
        )
    return f"Internal update posted for {iid} (status {status})."


def _asked(s: Script, role: str) -> bool:
    return any(name == f"ask_{role}" for name, _inp, _r in s.history)


def _fresh_incident(s: Script) -> bool:
    """True if get_incident was called after the most recent state-changing call."""
    last_get = max((i for i, h in enumerate(s.history) if h[0] == "get_incident"), default=-1)
    last_change = max(
        (i for i, h in enumerate(s.history) if h[0].startswith("ask_") or h[0] == "escalate_incident"), default=-1
    )
    return last_get > last_change


def _escalate(iid: str, reason: str) -> Call:
    return Call("escalate_incident", {"incident_id": iid, "reason": reason})


def coordinator_policy(s: Script) -> Call | str:
    """Drives the incident by its status, as the coordinator prompt describes."""
    iid = s.incident_id
    if not _fresh_incident(s):
        return Call("get_incident", {"incident_id": iid})
    incident = s.result("get_incident")
    status = incident["status"]
    diagnosis = incident.get("diagnosis") or {}

    if status == "open":
        return Call("ask_triage", {"incident_id": iid, "request": "Identify the affected service and severity."})
    if status == "triaging":
        if _asked(s, "triage"):
            return _escalate(iid, "Triage was inconclusive: no affected service could be identified.")
        return Call("ask_triage", {"incident_id": iid, "request": "Identify the affected service and severity."})
    if status == "investigating":
        if not diagnosis:
            if _asked(s, "diagnostics"):
                return _escalate(iid, "Diagnostics did not produce a diagnosis.")
            return Call("ask_diagnostics", {"incident_id": iid, "request": "Find the root cause, with evidence."})
        if diagnosis["confidence"] < 0.7:
            return _escalate(
                iid,
                f"Diagnosis confidence {diagnosis['confidence']:.2f} is too low to act on: {diagnosis['root_cause']}",
            )
        if not _asked(s, "remediation"):
            return Call("ask_remediation", {"incident_id": iid, "request": "Propose a remediation per the runbook."})
        return _escalate(iid, "No safe remediation: remediation declined or every proposal was denied by policy.")
    if status == "awaiting_approval":
        [pending] = [a for a in incident["approvals"] if a["status"] == "pending"] or [{}]
        return (
            f"{iid}: {incident.get('severity')} on {incident.get('service')}. Root cause: "
            f"{incident.get('root_cause')} Waiting for human approval of {pending.get('action')} "
            f"({pending.get('id')}, risk {pending.get('risk')})."
        )
    if status == "remediating":
        return f"{iid}: the approved action is running."
    if status == "verifying":
        verification = incident.get("verification") or {}
        if not verification:
            if _asked(s, "verification"):
                return f"{iid}: verification did not complete."
            return Call("ask_verification", {"incident_id": iid, "request": "The action ran. Verify recovery."})
        if not _asked(s, "communications"):
            return Call(
                "ask_communications",
                {"incident_id": iid, "request": "Recovery is verified. Update stakeholders and write the postmortem."},
            )
        return f"{iid}: verified but not resolved."
    if status == "failed":
        return _escalate(iid, incident.get("failure_reason") or "Remediation failed.")
    if status == "escalated":
        if not _asked(s, "communications"):
            return Call(
                "ask_communications",
                {"incident_id": iid, "request": "The incident was escalated. Post an internal update."},
            )
        return f"{iid} escalated to the owning team: {incident.get('escalation_reason')}"
    if status == "resolved":
        return f"{iid} resolved. Postmortem written."
    return f"{iid} is {status}."


POLICIES: dict[str, Policy] = {
    "coordinator": coordinator_policy,
    "triage": triage_policy,
    "diagnostics": diagnostics_policy,
    "remediation": remediation_policy,
    "verification": verification_policy,
    "communications": communications_policy,
}
