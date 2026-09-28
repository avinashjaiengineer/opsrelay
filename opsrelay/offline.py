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
    if s.called("update_triage"):
        _name, done, _result = next(h for h in reversed(s.history) if h[0] == "update_triage")
        return f"Triage complete for {iid}: {done['severity']} on {done['service']}. {done['summary']}"

    by_name = {h["service"]: h for h in health}
    text = f"{incident.get('title', '')} {incident.get('description', '')}".lower()
    service = incident.get("service") or next((n for n in by_name if n in text), None)
    if service is None:
        unhealthy = sorted((h for h in health if not h["healthy"]), key=lambda h: h["tier"])
        service = unhealthy[0]["service"] if unhealthy else None
    if service is None:
        return f"Could not identify an affected service for {iid}; every service reports healthy."
    h = by_name[service]
    severity = "sev1" if h["tier"] == 1 and h["error_rate"] >= 0.1 else "sev2" if h["tier"] == 1 else "sev3"
    summary = f"{service} degraded: error rate {h['error_rate']:.1%}, p99 {h['p99_latency_ms']} ms."
    return Call("update_triage", {"incident_id": iid, "service": service, "severity": severity, "summary": summary})


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
    if s.called("record_diagnosis"):
        return f"Diagnosis recorded for {iid}."

    latest = deploys["deployments"][-1] if deploys.get("deployments") else None
    evidence = [
        f"error_rate={metrics['error_rate']}",
        f"p99_latency_ms={metrics['p99_latency_ms']}",
        f"cpu_pct={metrics['cpu_pct']}",
        f"memory_pct={metrics['memory_pct']}",
    ]
    if metrics["error_rate"] >= 0.1 and latest and _recent(latest["at"]):
        category = "bad-deploy"
        root = f"Release {latest['version']} of {service}, deployed at {latest['at']}, introduced an exception."
        action = "Roll back to the previous version."
        evidence.append(f"deployed {latest['version']} at {latest['at']}")
    elif metrics["memory_pct"] >= 90:
        category = "memory-leak"
        root = f"{service} is exhausting memory and pods are being OOMKilled."
        action = "Rolling restart to reclaim memory; investigate the leak."
    elif metrics["cpu_pct"] >= 85:
        category = "saturation"
        root = f"{service} is CPU-saturated by traffic at {metrics['requests_per_min']} req/min."
        action = "Scale out replicas."
    else:
        category = "unknown"
        root = f"No clear cause found for {service}."
        action = "Escalate to the owning team."
    evidence.extend(logs[:2] if isinstance(logs, list) else [])
    return Call(
        "record_diagnosis",
        {
            "incident_id": iid,
            "root_cause": root,
            "category": category,
            "evidence": evidence,
            "recommended_action": action,
        },
    )


def remediation_policy(s: Script) -> Call | str:
    iid = s.incident_id
    incident = s.result("get_incident")
    if incident is None:
        return Call("get_incident", {"incident_id": iid})
    service = incident.get("service")
    if not service:
        return f"{iid} has no affected service; nothing to remediate."

    if "verify" in s.prompt.lower():
        metrics = s.result("get_metrics")
        if metrics is None:
            return Call("get_metrics", {"service": service})
        if not metrics["healthy"]:
            return f"{service} is still unhealthy after the action: {metrics}."
        if not s.called("mark_mitigated"):
            return Call(
                "mark_mitigated",
                {
                    "incident_id": iid,
                    "verification": f"error_rate={metrics['error_rate']}, p99={metrics['p99_latency_ms']} ms",
                },
            )
        return f"Verified: {service} is healthy. Incident {iid} marked mitigated."

    open_ = [a for a in incident.get("approvals", []) if a["status"] in ("pending", "approved")]
    if open_:
        return f"Action {open_[0]['action']} already proposed ({open_[0]['id']}, {open_[0]['status']})."
    category = incident.get("category", "unknown")
    if not s.called("get_runbook"):
        return Call("get_runbook", {"topic": category})
    proposed = s.result("propose_action")
    if proposed is not None:
        return f"Proposed remediation for {iid}: {proposed}."
    if category == "bad-deploy":
        action, extra = "rollback_deployment", {}
    elif category == "memory-leak":
        action, extra = "restart_service", {}
    elif category == "saturation":
        info = s.result("get_service_info")
        if info is None:
            return Call("get_service_info", {"service": service})
        action, extra = "scale_service", {"replicas": min(info["max_replicas"], info["replicas"] * 2)}
    else:
        return f"No safe automated remediation for category '{category}'; recommend escalation."
    return Call(
        "propose_action",
        {
            "incident_id": iid,
            "action": action,
            "service": service,
            "rationale": f"Runbook '{category}': {incident.get('recommended_action', '')}",
            **extra,
        },
    )


def communications_policy(s: Script) -> Call | str:
    iid = s.incident_id
    incident = s.result("get_incident")
    if incident is None:
        return Call("get_incident", {"incident_id": iid})
    status = incident["status"]
    posted = [inp["audience"] for name, inp, _r in s.history if name == "post_status_update"]

    if status == "mitigated":
        timeline = s.result("get_incident_timeline")
        if timeline is None:
            return Call("get_incident_timeline", {"incident_id": iid})
        if "internal" not in posted:
            return Call(
                "post_status_update",
                {
                    "incident_id": iid,
                    "audience": "internal",
                    "message": f"[{iid}] Mitigated. Cause: {incident.get('root_cause')} "
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
        if not s.called("resolve_incident"):
            lines = "\n".join(f"- {e['created_at']} {e['actor']}: {e['message']}" for e in timeline)
            postmortem = (
                f"# Postmortem: {incident['title']}\n\n"
                f"## Summary\n{incident.get('triage_summary', '')}\n\n"
                f"## Impact\nSeverity {incident.get('severity')}, service {incident.get('service')}.\n\n"
                f"## Root cause\n{incident.get('root_cause')}\n\n"
                f"## Resolution\n{incident.get('verification', '')}\n\n"
                f"## Timeline\n{lines}\n\n"
                "## Follow-ups\n- Add a regression test or alert that would have caught this earlier.\n"
            )
            return Call("resolve_incident", {"incident_id": iid, "postmortem": postmortem})
        return f"Stakeholders updated and incident {iid} resolved with a postmortem."

    if "internal" not in posted:
        detail = incident.get("escalation_reason") or incident.get("triage_summary") or incident["title"]
        return Call(
            "post_status_update",
            {"incident_id": iid, "audience": "internal", "message": f"[{iid}] Status: {status}. {detail}"},
        )
    return f"Internal update posted for {iid} (status {status})."


def _asked(s: Script, role: str, word: str = "") -> bool:
    return any(name == f"ask_{role}" and word.lower() in inp.get("request", "").lower() for name, inp, _r in s.history)


def _fresh_incident(s: Script) -> bool:
    """True if get_incident was called after the most recent delegation."""
    last_get = max((i for i, h in enumerate(s.history) if h[0] == "get_incident"), default=-1)
    last_ask = max((i for i, h in enumerate(s.history) if h[0].startswith("ask_")), default=-1)
    return last_get > last_ask


def coordinator_policy(s: Script) -> Call | str:
    iid = s.incident_id
    if "was decided" not in s.prompt:
        for role, request in (
            ("triage", "Triage this incident: identify the affected service and severity."),
            ("diagnostics", "Find the root cause, with evidence."),
            ("remediation", "Propose a remediation per the runbook."),
        ):
            if not _asked(s, role):
                return Call(f"ask_{role}", {"incident_id": iid, "request": request})

    if s.called("escalate_incident"):
        reason = next(inp["reason"] for name, inp, _r in reversed(s.history) if name == "escalate_incident")
        if not _asked(s, "communications"):
            return Call(
                "ask_communications",
                {"incident_id": iid, "request": "Post an internal update: the incident was escalated."},
            )
        return f"{iid} escalated to the owning team: {reason}"

    if not _fresh_incident(s):
        return Call("get_incident", {"incident_id": iid})
    incident = s.result("get_incident")
    status = incident["status"]
    pending = [a for a in incident["approvals"] if a["status"] == "pending"]
    if pending:
        a = pending[0]
        return (
            f"{iid}: {(incident.get('severity') or '').upper()} on {incident.get('service')}. "
            f"Root cause: {incident.get('root_cause')} Waiting for human approval of {a['action']} "
            f"({a['id']}, risk {a['risk']})."
        )
    if status == "resolved":
        return f"{iid} mitigated and resolved. {s.result('ask_communications')}"
    if status == "mitigated":
        return Call(
            "ask_communications",
            {"incident_id": iid, "request": "Update stakeholders, write the postmortem, and resolve."},
        )

    decided = [a for a in incident["approvals"] if a["status"] in ("executed", "rejected", "failed")]
    last = decided[-1] if decided else None
    if last is None:
        reason = "No safe automated remediation found."
    elif last["status"] == "executed":
        if not _asked(s, "remediation", "verify"):
            return Call(
                "ask_remediation",
                {"incident_id": iid, "request": f"{last['action']} ran. Verify recovery of {last['service']}."},
            )
        reason = f"{last['action']} ran but {incident.get('service')} did not recover."
    else:
        reason = f"Remediation {last['action']} was {last['status']}" + (
            f" ({last['note']})" if last.get("note") else ""
        )
    return Call("escalate_incident", {"incident_id": iid, "reason": reason})


POLICIES: dict[str, Policy] = {
    "coordinator": coordinator_policy,
    "triage": triage_policy,
    "diagnostics": diagnostics_policy,
    "remediation": remediation_policy,
    "communications": communications_policy,
}
