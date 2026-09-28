"""Durable background work: a job queue in the shared store, and a worker that drains it.

    API request -> enqueue job (store) -> worker claims it under a lease -> coordinator runs
                                                     |
                             process dies -> lease expires -> another worker picks it up

A job is a record, claimed by compare-and-set on its revision with a lease that a heartbeat keeps
extending while it runs. If the process stops, the lease runs out and any worker (in this process
after a restart, or another replica) takes the job over. Re-running a coordination job is safe:
the coordinator acts on the incident's current state and never repeats a step that succeeded.
A job that keeps failing is dead-lettered and its incident handed to a person.

Two ways to run jobs:

- A worker thread in the coordinator process polls the store (local, EC2, Docker Compose). It also
  runs `approvals.recover` periodically.
- With OPSRELAY_JOB_QUEUE_URL set (AWS), enqueuing also sends the job id to an SQS queue; a Lambda
  consumes it and calls the coordinator's `run_job` action, which runs that job synchronously. No
  long-running thread is needed; a scheduled Lambda calls `recover`, which also re-queues jobs
  whose lease expired. SQS retries and its dead-letter queue sit on top of the job's own lease.
"""

import json
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable

from . import approvals
from .config import get_settings
from .deadletter import dead_letter
from .store import Record, Store
from .store.base import new_record

log = logging.getLogger(__name__)
KIND = "job"


def enqueue(store: Store, incident_id: str, prompt: str, *, action: str = "coordinate") -> Record:
    job = new_record(
        KIND,
        f"job-{uuid.uuid4().hex[:12]}",
        "queued",
        action=action,
        incident_id=incident_id,
        prompt=prompt,
        attempts=0,
        max_attempts=get_settings().job_max_attempts,
        lease_until=0.0,
        owner=None,
        error=None,
    )
    store.put_record(job)
    store.record(incident_id, "platform", "job.queued", f"{action} queued ({job['id']})", {"job_id": job["id"]})
    _send_to_queue(job["id"])
    return job


def _send_to_queue(job_id: str) -> None:
    settings = get_settings()
    if settings.job_queue_url:
        import boto3

        boto3.client("sqs", region_name=settings.aws_region).send_message(
            QueueUrl=settings.job_queue_url, MessageBody=json.dumps({"job_id": job_id})
        )


def requeue_expired(store: Store) -> list[str]:
    """Re-send claimable jobs (still queued, or their worker's lease expired) to the SQS queue."""
    now = time.time()
    ids = [j["id"] for j in store.list_records(KIND) if claimable(j, now) and j["attempts"] < j["max_attempts"]]
    for job_id in ids:
        _send_to_queue(job_id)
    return ids


def claimable(job: Record, now: float) -> bool:
    return job["status"] == "queued" or (job["status"] == "running" and job.get("lease_until", 0) < now)


def claim(store: Store, job: Record, owner: str) -> Record | None:
    """Claim one job if it's claimable (queued, or running with an expired lease)."""
    now = time.time()
    if not claimable(job, now):
        return None
    lease = get_settings().job_lease_seconds
    taken = store.move_record(
        KIND,
        job["id"],
        job["rev"],
        {"status": "running", "owner": owner, "lease_until": now + lease, "attempts": job["attempts"] + 1},
    )
    if taken and job["status"] == "running":
        store.record(
            job["incident_id"],
            "platform",
            "job.recovered",
            f"{job['action']} taken over from {job.get('owner')} after its lease expired",
            {"job_id": job["id"], "previous_owner": job.get("owner")},
        )
    return taken


def claim_next(store: Store, owner: str) -> Record | None:
    for job in reversed(store.list_records(KIND)):  # oldest first
        taken = claim(store, job, owner)
        if taken:
            return taken
    return None


class Worker:
    """Drains the job queue in a background thread. One per coordinator process is enough; several
    (in one or many processes) are safe, because claims are compare-and-set."""

    def __init__(
        self,
        service_factory: Callable[[], object],
        *,
        on_busy: Callable[[Record], object] | None = None,
        on_idle: Callable[[object], None] | None = None,
    ):
        self.service_factory = service_factory
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.on_busy = on_busy
        self.on_idle = on_idle
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_recovery = 0.0

    def start(self) -> "Worker":
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="opsrelay-worker", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        settings = get_settings()
        while not self._stop.is_set():
            try:
                self.recover_if_due()
                if not self.run_once():
                    self._stop.wait(settings.worker_poll_seconds)
            except Exception:  # noqa: BLE001 - the worker must keep running
                log.exception("worker iteration failed")
                self._stop.wait(settings.worker_poll_seconds)

    def recover_if_due(self) -> None:
        if time.time() - self._last_recovery < get_settings().recovery_interval_seconds:
            return
        self._last_recovery = time.time()
        svc = self.service_factory()
        touched = approvals.recover(svc.store, svc.env)
        if touched:
            log.info("recovered interrupted remediations: %s", touched)

    def run_once(self) -> bool:
        """Claim and run one job. False if there was nothing to do."""
        svc = self.service_factory()
        job = claim_next(svc.store, self.owner)
        if job is None:
            return False
        self._run(svc, job)
        return True

    def run_job(self, job_id: str) -> Record:
        """Run one specific job now, if it's claimable (the SQS path). Returns its outcome."""
        svc = self.service_factory()
        job = svc.store.get_record(KIND, job_id)
        if job is None:
            return {"job_id": job_id, "status": "unknown"}
        if job["status"] in ("done", "failed"):
            return {"job_id": job_id, "status": job["status"]}
        taken = claim(svc.store, job, self.owner)
        if taken is None:
            return {"job_id": job_id, "status": "in_progress", "owner": job.get("owner")}
        self._run(svc, taken)
        return {"job_id": job_id, "status": svc.store.get_record(KIND, job_id)["status"]}

    def _run(self, svc, job: Record) -> None:  # noqa: ANN001
        token = self.on_busy(job) if self.on_busy else None
        beat = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(svc.store, job, beat), daemon=True)
        heartbeat.start()
        try:
            svc.run_coordinator(job["incident_id"], job["prompt"])
        except Exception as e:  # noqa: BLE001 - recorded on the job and in the audit log
            beat.set()
            heartbeat.join()
            self._failed(svc.store, job, e)
        else:
            beat.set()
            heartbeat.join()
            self._finish(svc.store, job["id"], {"status": "done", "error": None, "lease_until": 0.0})
        finally:
            if self.on_idle:
                self.on_idle(token)

    def _heartbeat(self, store: Store, job: Record, stop: threading.Event) -> None:
        lease = get_settings().job_lease_seconds
        while not stop.wait(lease / 3):
            current = store.get_record(KIND, job["id"])
            if not current or current.get("owner") != self.owner:
                log.warning("lost the lease on %s", job["id"])
                return
            store.move_record(KIND, job["id"], current["rev"], {"lease_until": time.time() + lease})

    def _finish(self, store: Store, job_id: str, updates: Record) -> None:
        for _ in range(5):
            current = store.get_record(KIND, job_id)
            if current is None or current.get("owner") != self.owner:
                return  # someone took it over; they own the outcome now
            if store.move_record(KIND, job_id, current["rev"], updates):
                return

    def _failed(self, store: Store, job: Record, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        log.warning("job %s failed (attempt %s): %s", job["id"], job["attempts"], message)
        if job["attempts"] < job["max_attempts"]:
            self._finish(store, job["id"], {"status": "queued", "error": message, "lease_until": 0.0})
            store.record(
                job["incident_id"],
                "platform",
                "job.retry",
                f"{job['action']} attempt {job['attempts']} failed: {message}",
                {"job_id": job["id"]},
            )
            return
        self._finish(store, job["id"], {"status": "failed", "error": message, "lease_until": 0.0})
        dead_letter(store, job["incident_id"], "coordinator", job["prompt"], [message], message)


_worker: Worker | None = None
_worker_lock = threading.Lock()


def ensure_worker(service_factory: Callable[[], object], **callbacks) -> Worker:  # noqa: ANN003
    """Start this process's worker once."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = Worker(service_factory, **callbacks).start()
        return _worker


def stop_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.stop()
            _worker = None
