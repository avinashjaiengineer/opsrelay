"""The execution engine: the only code that changes infrastructure.

It runs an action only for an approval the approval engine has approved, and only once. Each
execution is a record keyed by an idempotency key (incident, action, service, parameters) and
held under a lease:

    no record                      -> claim it with a lease, run the action, record the result
    done                           -> return the stored result (a retry or duplicate decision)
    running, lease still valid     -> in progress elsewhere; don't start it again
    running, lease expired         -> the executor that claimed it stopped mid-action: take the
                                      lease over and ask the environment what actually happened
                                      (reconcile) before doing anything:
                                        applied      -> record success, don't repeat it
                                        not applied  -> run it now
                                        unknown      -> fail safe: record it and hand it to a person

The environment is told the idempotency key, so a real connector can tag the change with it
(a deployment id, a change ticket) and find it again when reconciling.
"""

import os
import socket
import time
import uuid

from .config import get_settings
from .environment import Environment
from .store import Record, Store, now_iso
from .store.base import new_record, sha256

KIND = "execution"


def idempotency_key(approval: Record) -> str:
    params = approval.get("params") or {}
    suffix = sha256(params)[:8] if params else "noparams"
    return f"{approval['incident_id']}:{approval['action']}:{approval['service']}:{suffix}"


def _owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def execute(store: Store, env: Environment, approval: Record) -> Record:
    """Run the approved action at most once.

    Returns {"ok", "detail", "key", "replayed"}, or {"in_progress": True, ...} if another executor
    currently holds the lease.
    """
    if approval.get("status") != "approved":
        raise ValueError(f"Approval {approval['id']} is {approval.get('status')}; only approved actions run")
    key = idempotency_key(approval)
    lease = get_settings().execution_lease_seconds
    owner = _owner()
    claim = new_record(
        KIND,
        key,
        "running",
        approval_id=approval["id"],
        incident_id=approval["incident_id"],
        action=approval["action"],
        service=approval["service"],
        params=approval.get("params") or {},
        owner=owner,
        attempt=1,
        lease_until=time.time() + lease,
        started_at=now_iso(),
    )
    if store.put_record(claim):
        return _run(store, env, approval, claim)

    existing = store.get_record(KIND, key) or {}
    if existing.get("status") == "done":
        store.record(
            approval["incident_id"],
            "platform",
            "execution.deduplicated",
            f"{approval['action']} on {approval['service']} already ran ({key}); not running it again",
            {"idempotency_key": key, "approval_id": approval["id"]},
        )
        return {**existing["result"], "key": key, "replayed": True}
    if existing.get("lease_until", 0) > time.time():
        return {"ok": None, "in_progress": True, "key": key, "detail": f"running under {existing.get('owner')}"}

    taken = store.move_record(
        KIND,
        key,
        existing["rev"],
        {"owner": owner, "attempt": existing.get("attempt", 1) + 1, "lease_until": time.time() + lease},
    )
    if taken is None:
        return {"ok": None, "in_progress": True, "key": key, "detail": "another executor took over"}
    state = env.reconcile(approval["action"], approval["service"], approval.get("params") or {}, key)
    store.record(
        approval["incident_id"],
        "platform",
        "execution.recovering",
        f"{approval['action']} on {approval['service']} was interrupted ({existing.get('owner')} stopped); "
        f"the environment reports it {state.replace('_', ' ')}",
        {"idempotency_key": key, "previous_owner": existing.get("owner"), "reconciled": state},
    )
    if state == "not_applied":
        return _run(store, env, approval, taken)
    if state == "applied":
        result = {"ok": True, "detail": f"{approval['action']} on {approval['service']} had already been applied"}
    else:
        result = {
            "ok": False,
            "detail": f"Could not confirm whether {approval['action']} on {approval['service']} ran before the "
            "executor stopped; a person must check",
        }
    return _finish(store, approval, taken, result, reconciled=state)


def _run(store: Store, env: Environment, approval: Record, claim: Record) -> Record:
    key = claim["id"]
    params = approval.get("params") or {}
    store.record(
        approval["incident_id"],
        "platform",
        "tool.invoked",
        f"{approval['action']} on {approval['service']}",
        {"idempotency_key": key, "approval_id": approval["id"], "params": params, "attempt": claim["attempt"]},
        input=params,
    )
    try:
        result = env.execute(approval["action"], approval["service"], params, idempotency_key=key)
    except Exception as e:  # noqa: BLE001 - any connector failure is a failed execution, not a crash
        result = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    return _finish(store, approval, claim, result)


def _finish(store: Store, approval: Record, claim: Record, result: Record, reconciled: str | None = None) -> Record:
    key = claim["id"]
    done = store.move_record(
        KIND,
        key,
        claim["rev"],
        {"status": "done", "result": result, "finished_at": now_iso(), "reconciled": reconciled},
    )
    store.record(
        approval["incident_id"],
        "platform",
        "tool.completed" if result["ok"] else "tool.failed",
        result["detail"],
        {"idempotency_key": key, "approval_id": approval["id"], "lease_lost": done is None},
        output=result,
    )
    return {**result, "key": key, "replayed": False}
