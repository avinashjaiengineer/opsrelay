"""The execution engine: the only code that changes infrastructure.

It runs an action only for an approval the approval engine has approved, and only once:
each execution has an idempotency key (incident, action, service, parameters), claimed in the
store before the action runs. A retry, a duplicated event or a second approver gets the first
result back instead of running the action again.
"""

from .environment import Environment
from .store import Record, Store, now_iso
from .store.base import sha256


def idempotency_key(approval: Record) -> str:
    params = approval.get("params") or {}
    suffix = sha256(params)[:8] if params else "noparams"
    return f"{approval['incident_id']}:{approval['action']}:{approval['service']}:{suffix}"


def execute(store: Store, env: Environment, approval: Record) -> Record:
    """Run the approved action once. Returns {"ok", "detail", "key", "replayed"}."""
    if approval.get("status") != "approved":
        raise ValueError(f"Approval {approval['id']} is {approval.get('status')}; only approved actions run")
    key = idempotency_key(approval)
    claimed = store.claim_execution(
        {"key": key, "approval_id": approval["id"], "state": "running", "started_at": now_iso()}
    )
    if not claimed:
        previous = store.get_execution(key) or {}
        result = previous.get("result") or {"ok": False, "detail": "the same action is already running"}
        store.record(
            approval["incident_id"],
            "platform",
            "execution.deduplicated",
            f"{approval['action']} on {approval['service']} already ran ({key}); not running it again",
            {"idempotency_key": key, "approval_id": approval["id"]},
        )
        return {**result, "key": key, "replayed": True}

    store.record(
        approval["incident_id"],
        "platform",
        "tool.invoked",
        f"{approval['action']} on {approval['service']}",
        {"idempotency_key": key, "approval_id": approval["id"], "params": approval.get("params") or {}},
        input=approval.get("params") or {},
    )
    try:
        result = env.execute(approval["action"], approval["service"], approval.get("params") or {})
    except Exception as e:  # noqa: BLE001 - any connector failure is a failed execution, not a crash
        result = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    store.finish_execution(key, {"state": "done", "result": result, "finished_at": now_iso()})
    store.record(
        approval["incident_id"],
        "platform",
        "tool.completed" if result["ok"] else "tool.failed",
        result["detail"],
        {"idempotency_key": key, "approval_id": approval["id"]},
        output=result,
    )
    return {**result, "key": key, "replayed": False}
