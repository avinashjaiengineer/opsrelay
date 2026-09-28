"""The coordinator, served as an AgentCore Runtime (protocol HTTP: POST /invocations, GET /ping).

Payloads are JSON objects with an "action":

    {"action": "open_incident", "title": "...", "description": "...", "service": null, "external_ref": null}
    {"action": "simulate", "scenario": "bad-deploy"}
    {"action": "decide_approval", "approval_id": "apr-...", "approve": true, "approver": "jane@corp", "note": "..."}
    {"action": "get_incident", "incident_id": "inc-..."}
    {"action": "list_incidents"}
    {"action": "list_approvals", "status": "pending"}
    {"action": "health"}
    {"action": "verify_audit", "incident_id": "inc-..."}      # check the audit hash chain
    {"action": "list_dead_letters"}                           # requests to agents that stayed unavailable
    {"action": "get_policy"}                                  # the loaded remediation policy
    {"action": "test_policy", "action_name": "scale_service", "service": "...", "parameters": {"replicas": 4}}
    {"action": "get_contracts"}                               # lifecycle, agent contracts, transition owners

Add "async": true to open_incident, simulate or decide_approval to return at once: the work is
queued as a durable job (opsrelay.jobs) and a worker runs it (the runtime reports HealthyBusy
while it does). A job survives a restart. Poll with get_incident.
"""

import logging
import threading
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError

from .. import jobs
from ..approvals import ApprovalError
from ..lifecycle import IllegalTransition
from ..service import IncidentService, decision_prompt, new_incident_prompt

log = logging.getLogger(__name__)
app = BedrockAgentCoreApp()

_service: IncidentService | None = None
_service_lock = threading.Lock()


def service() -> IncidentService:
    global _service
    with _service_lock:
        if _service is None:
            _service = IncidentService()
        return _service


def _require(payload: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if not payload.get(k)]
    if missing:
        raise ValueError(f"missing field(s): {', '.join(missing)}")


def start_worker():  # noqa: ANN201
    """Start this process's job worker, reporting HealthyBusy to AgentCore while a job runs."""
    return jobs.ensure_worker(
        service,
        on_busy=lambda job: app.add_async_task(job["action"], {"incident_id": job["incident_id"], "job": job["id"]}),
        on_idle=lambda task_id: task_id is not None and app.complete_async_task(task_id),
    )


def _in_background(incident_id: str, prompt: str) -> None:
    """Queue coordination as a durable job; the worker runs it, and a restart doesn't lose it."""
    jobs.enqueue(service().store, incident_id, prompt)
    start_worker()


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    svc = service()
    action = payload.get("action")
    background = bool(payload.get("async"))

    if action == "open_incident":
        _require(payload, "title")
        kwargs = {
            "title": payload["title"],
            "description": payload.get("description", ""),
            "source": payload.get("source", "api"),
            "service": payload.get("service"),
            "external_ref": payload.get("external_ref"),
        }
        if background:
            opened = svc.open_incident(**kwargs, run=False)
            incident = opened["incident"]
            if not opened.get("deduplicated"):
                _in_background(incident["id"], new_incident_prompt(incident))
            return {"incident": incident, "status": "processing"}
        return svc.open_incident(**kwargs)

    if action == "simulate":
        _require(payload, "scenario")
        if background:
            result = svc.simulate(payload["scenario"], run=False)
            incident = result["incident"]
            _in_background(incident["id"], new_incident_prompt(incident))
            return {**result, "status": "processing"}
        return svc.simulate(payload["scenario"])

    if action == "decide_approval":
        _require(payload, "approval_id", "approver")
        if "approve" not in payload:
            raise ValueError("missing field: approve (true or false)")
        kwargs = {
            "approval_id": payload["approval_id"],
            "approve": bool(payload["approve"]),
            "approver": payload["approver"],
            "note": payload.get("note"),
        }
        if background:
            decided = svc.decide_approval(**kwargs, run=False)
            approval = decided["approval"]
            _in_background(approval["incident_id"], decision_prompt(approval))
            return {**decided, "status": "processing"}
        return svc.decide_approval(**kwargs)

    if action == "get_incident":
        _require(payload, "incident_id")
        return svc.get_incident(payload["incident_id"])
    if action == "list_incidents":
        return {"incidents": svc.list_incidents(int(payload.get("limit", 50)))}
    if action == "list_approvals":
        return {"approvals": svc.list_approvals(payload.get("status", "pending"))}
    if action == "health":
        return {"services": svc.health()}
    if action == "verify_audit":
        _require(payload, "incident_id")
        return svc.verify_audit(payload["incident_id"])
    if action == "list_dead_letters":
        return {"dead_letters": svc.dead_letters(int(payload.get("limit", 50)))}
    if action == "get_policy":
        return {"policy": svc.policy()}
    if action == "test_policy":
        _require(payload, "action_name", "service")
        return {
            "decision": svc.test_policy(
                payload["action_name"],
                payload["service"],
                payload.get("parameters") or {},
                float(payload.get("confidence", 0.95)),
            )
        }
    if action == "get_contracts":
        return svc.contracts()
    raise ValueError(f"unknown action '{action}'")


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"error": "payload must be a JSON object"}
    try:
        return handle(payload)
    except (ValueError, KeyError, ApprovalError, IllegalTransition, ValidationError) as e:
        return {"error": str(e).strip("'\"")}


def serve(port: int = 8080) -> None:
    start_worker()  # resumes queued or interrupted work after a restart
    app.run(port=port)
