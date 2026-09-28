"""Durable background work, and authenticated agent-to-agent calls."""

import time

from starlette.testclient import TestClient

from opsrelay import jobs
from opsrelay.config import get_settings


def _worker(service):
    return jobs.Worker(lambda: service)


def test_queued_job_runs_the_coordinator(service):
    service.env.inject("bad-deploy")
    incident = service.open_incident("checkout-api 5xx", "errors", run=False)["incident"]
    job = jobs.enqueue(service.store, incident["id"], f"New incident {incident['id']}. Coordinate the response.")

    assert _worker(service).run_once() is True

    assert service.store.get_record("job", job["id"])["status"] == "done"
    assert service.store.get_incident(incident["id"])["status"] == "awaiting_approval"
    assert _worker(service).run_once() is False  # nothing left


def test_a_dead_workers_job_is_taken_over(service):
    service.env.inject("bad-deploy")
    incident = service.open_incident("checkout-api 5xx", "errors", run=False)["incident"]
    job = jobs.enqueue(service.store, incident["id"], f"New incident {incident['id']}. Coordinate the response.")
    # Another worker claimed it and died: the job is "running" but its lease has expired.
    service.store.move_record(
        "job", job["id"], 0, {"status": "running", "owner": "dead-host", "lease_until": time.time() - 1, "attempts": 1}
    )

    assert _worker(service).run_once() is True

    done = service.store.get_record("job", job["id"])
    assert (done["status"], done["attempts"]) == ("done", 2)
    assert "job.recovered" in [e["kind"] for e in service.store.list_events(incident["id"])]
    assert service.store.get_incident(incident["id"])["status"] == "awaiting_approval"


def test_a_live_lease_is_left_alone(service):
    incident = service.open_incident("x", "y", run=False)["incident"]
    job = jobs.enqueue(service.store, incident["id"], "prompt")
    service.store.move_record(
        "job", job["id"], 0, {"status": "running", "owner": "busy-host", "lease_until": time.time() + 60}
    )
    assert _worker(service).run_once() is False


def test_a_job_that_keeps_failing_is_dead_lettered(service, monkeypatch):
    incident = service.open_incident("checkout-api 5xx", "errors", run=False)["incident"]
    job = jobs.enqueue(service.store, incident["id"], "prompt")

    def broken(incident_id, prompt):
        raise RuntimeError("model endpoint unreachable")

    monkeypatch.setattr(service, "run_coordinator", broken)
    worker = _worker(service)
    for _ in range(get_settings().job_max_attempts):
        assert worker.run_once() is True

    failed = service.store.get_record("job", job["id"])
    assert (failed["status"], failed["attempts"]) == ("failed", 3)
    [letter] = service.dead_letters()
    assert letter["agent"] == "coordinator" and "unreachable" in letter["error"]
    kinds = [e["kind"] for e in service.store.list_events(incident["id"])]
    assert kinds.count("job.retry") == 2 and "agent.unavailable" in kinds


def test_specialists_require_the_a2a_token(monkeypatch):
    monkeypatch.setenv("OPSRELAY_A2A_TOKEN", "s3cret-token")
    get_settings.cache_clear()
    from opsrelay.runtime.specialist import build_app

    client = TestClient(build_app("triage", "http://127.0.0.1:9001/"))
    request = {"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {}}

    assert client.get("/.well-known/agent-card.json").status_code == 200  # the card stays public
    assert client.post("/", json=request).status_code == 401
    assert client.post("/", json=request, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/", json=request, headers={"Authorization": "Bearer s3cret-token"}).status_code != 401


def test_the_a2a_client_presents_the_token(monkeypatch):
    monkeypatch.setenv("OPSRELAY_A2A_TOKEN", "s3cret-token")
    get_settings.cache_clear()
    from opsrelay.remote import resolve_endpoint

    assert resolve_endpoint("http://triage:9000/")[2] == {"Authorization": "Bearer s3cret-token"}
    arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/opsrelay_triage-x"
    assert "Authorization" not in resolve_endpoint(arn)[2]  # AgentCore calls are SigV4-signed instead
