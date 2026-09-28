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
    {"action": "whoami"}                                      # the authenticated caller and roles
    {"action": "ingest_alert", "message": {...}}              # CloudWatch alarm event / SNS / Alertmanager
    {"action": "run_job", "job_id": "job-..."}                # run one queued job now (the SQS worker path)
    {"action": "recover"}                                     # finish interrupted work (scheduled on AWS)
    {"action": "get_metrics"}                                 # MTTA, MTTR, stage times, agent and policy stats
    {"action": "list_policies"} / {"action": "propose_policy", "text": "<yaml>"}
    {"action": "review_policy", "version": "v2", "approve": true} / {"action": "activate_policy", "version": "v2"}

Callers are authenticated per OPSRELAY_AUTH_MODE (opsrelay.auth) and every action is authorized by
role (opsrelay.rbac). With authentication on, a decision's approver is the caller, never a name in
the payload.

Add "async": true to open_incident, simulate or decide_approval to return at once: the work is
queued as a durable job (opsrelay.jobs) and a worker runs it (the runtime reports HealthyBusy
while it does). A job survives a restart. Poll with get_incident.
"""

import hmac
import logging
import threading
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import approvals, auth, jobs, ops_metrics, policy_admin, rbac, secrets
from ..approvals import ApprovalError
from ..config import get_settings
from ..intake.alerts import UnrecognizedAlert
from ..intake.sqs import SqsIntake
from ..lifecycle import IllegalTransition
from ..service import IncidentService, decision_prompt, new_incident_prompt

log = logging.getLogger(__name__)
app = BedrockAgentCoreApp()
app.add_middleware(auth.AuthMiddleware)  # OPSRELAY_AUTH_MODE decides whether it requires a token

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


_intake: SqsIntake | None = None


def start_worker():  # noqa: ANN201
    """Start this process's job worker (reporting HealthyBusy to AgentCore while a job runs), and,
    with OPSRELAY_INTAKE_QUEUE_URL set, its SQS alert consumer."""
    global _intake
    settings = get_settings()
    if settings.intake_queue_url and _intake is None:
        _intake = SqsIntake(settings.intake_queue_url, service, region=settings.aws_region).start()
    if settings.job_queue_url:
        return None  # jobs arrive through SQS and the run_job action; no polling thread needed
    return jobs.ensure_worker(
        service,
        on_busy=lambda job: app.add_async_task(job["action"], {"incident_id": job["incident_id"], "job": job["id"]}),
        on_idle=lambda task_id: task_id is not None and app.complete_async_task(task_id),
    )


async def alerts_webhook(request: Request) -> JSONResponse:
    """POST /alerts: Alertmanager webhooks (or CloudWatch/SNS JSON). Needs
    `Authorization: Bearer <OPSRELAY_WEBHOOK_TOKEN>`; disabled when no token is configured."""
    expected = secrets.resolve(get_settings().webhook_token)
    if not expected:
        return JSONResponse({"error": "the alert webhook is disabled (set OPSRELAY_WEBHOOK_TOKEN)"}, status_code=404)
    presented = request.headers.get("authorization", "")
    if not hmac.compare_digest(presented.encode(), f"Bearer {expected}".encode()):
        return JSONResponse({"error": "invalid webhook token"}, status_code=401)
    try:
        results = await run_in_threadpool(service().ingest, await request.json())
    except (UnrecognizedAlert, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    start_worker()
    return JSONResponse({"results": results})


app.router.routes.insert(0, Route("/alerts", alerts_webhook, methods=["POST"]))


def _in_background(incident_id: str, prompt: str) -> None:
    """Queue coordination as a durable job; the worker runs it, and a restart doesn't lose it."""
    jobs.enqueue(service().store, incident_id, prompt)
    start_worker()


def _actor(principal: auth.Principal, payload: dict[str, Any], field: str) -> str:
    """Who is acting: the authenticated principal, or (with auth off) the name given in `field`."""
    if principal.verified:
        return principal.name
    _require(payload, field)
    return payload[field]


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    svc = service()
    action = payload.get("action")
    background = bool(payload.get("async"))
    principal = auth.current()
    rbac.authorize(principal, str(action))

    if action == "whoami":
        return {"principal": principal.public()}

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
        _require(payload, "approval_id")
        if "approve" not in payload:
            raise ValueError("missing field: approve (true or false)")
        approval = svc.store.get_approval(payload["approval_id"])
        if approval is None:
            raise KeyError(f"Unknown approval {payload['approval_id']}")
        role = None
        if principal.verified:
            incident = svc.store.get_incident(approval["incident_id"]) or {}
            role = rbac.authorize_decision(
                principal, approval["risk"], approval.get("severity") or incident.get("severity")
            )
        kwargs = {
            "approval_id": payload["approval_id"],
            "approve": bool(payload["approve"]),
            "approver": _actor(principal, payload, "approver"),
            "note": payload.get("note"),
            "role": role,
            "verified": principal.verified,
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
    if action == "ingest_alert":
        if "message" not in payload:
            raise ValueError(
                "missing field: message (a CloudWatch alarm event, SNS notification or Alertmanager payload)"
            )
        results = svc.ingest(payload["message"])
        if any(r.get("job_id") for r in results):
            start_worker()
        return {"results": results}
    if action == "run_job":
        _require(payload, "job_id")
        return jobs.Worker(service).run_job(payload["job_id"])
    if action == "recover":
        return {"remediations": approvals.recover(svc.store, svc.env), "requeued_jobs": jobs.requeue_expired(svc.store)}
    if action == "get_metrics":
        return {"metrics": ops_metrics.compute(svc.store, int(payload.get("limit", 200)))}
    if action == "get_contracts":
        return svc.contracts()
    if action == "list_policies":
        return {"policies": policy_admin.history(svc.store)}
    if action == "propose_policy":
        _require(payload, "text")
        return {
            "policy": policy_admin.propose(
                svc.store, payload["text"], _actor(principal, payload, "by"), payload.get("note", "")
            )
        }
    if action == "review_policy":
        _require(payload, "version")
        if "approve" not in payload:
            raise ValueError("missing field: approve (true or false)")
        reviewer = _actor(principal, payload, "by")
        return {
            "policy": policy_admin.review(
                svc.store, payload["version"], reviewer, bool(payload["approve"]), payload.get("note", "")
            )
        }
    if action == "activate_policy":
        _require(payload, "version")
        return {"policy": policy_admin.activate(svc.store, payload["version"], _actor(principal, payload, "by"))}
    raise ValueError(f"unknown action '{action}'")


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"error": "payload must be a JSON object"}
    try:
        return handle(payload)
    except rbac.Forbidden as e:
        return {"error": f"forbidden: {e}"}
    except (ValueError, KeyError, ApprovalError, IllegalTransition, ValidationError, UnrecognizedAlert) as e:
        return {"error": str(e).strip("'\"")}


def serve(port: int = 8080) -> None:
    start_worker()  # resumes queued or interrupted work after a restart
    app.run(port=port)
