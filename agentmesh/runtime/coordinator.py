"""The coordinator, served as an AgentCore Runtime (protocol HTTP: POST /invocations, GET /ping).

Payloads are JSON objects with an "action":

    {"action": "open_incident", "title": "...", "description": "...", "service": null, "external_ref": null}
    {"action": "simulate", "scenario": "bad-deploy"}
    {"action": "decide_approval", "approval_id": "apr-...", "approve": true, "approver": "jane@corp", "note": "..."}
    {"action": "get_incident", "incident_id": "inc-..."}
    {"action": "list_incidents"}
    {"action": "list_approvals", "status": "pending"}
    {"action": "health"}

Add "async": true to open_incident, simulate or decide_approval to return at once and let the
agents work in the background (the runtime reports HealthyBusy until they finish); poll with
get_incident.
"""

import logging
import threading
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from ..approvals import ApprovalError
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


def _in_background(name: str, fn, **kwargs) -> None:  # noqa: ANN001
    task_id = app.add_async_task(name, {k: str(v)[:100] for k, v in kwargs.items()})

    def run() -> None:
        try:
            fn(**kwargs)
        except Exception:  # noqa: BLE001 - logged and recorded in the incident audit log
            log.exception("background %s failed", name)
        finally:
            app.complete_async_task(task_id)

    threading.Thread(target=run, name=f"agentmesh-{name}", daemon=True).start()


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
                _in_background(
                    "coordinate",
                    svc.run_coordinator,
                    incident_id=incident["id"],
                    prompt=new_incident_prompt(incident),
                )
            return {"incident": incident, "status": "processing"}
        return svc.open_incident(**kwargs)

    if action == "simulate":
        _require(payload, "scenario")
        if background:
            result = svc.simulate(payload["scenario"], run=False)
            incident = result["incident"]
            _in_background(
                "coordinate",
                svc.run_coordinator,
                incident_id=incident["id"],
                prompt=new_incident_prompt(incident),
            )
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
            _in_background(
                "continue",
                svc.run_coordinator,
                incident_id=approval["incident_id"],
                prompt=decision_prompt(approval),
            )
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
    raise ValueError(f"unknown action '{action}'")


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"error": "payload must be a JSON object"}
    try:
        return handle(payload)
    except (ValueError, KeyError, ApprovalError) as e:
        return {"error": str(e).strip("'\"")}


def serve(port: int = 8080) -> None:
    app.run(port=port)
