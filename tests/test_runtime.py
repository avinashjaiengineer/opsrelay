"""The coordinator's AgentCore HTTP contract: POST /invocations and GET /ping."""

import time

import httpx
import pytest
from starlette.testclient import TestClient

from agentmesh.runtime import coordinator
from agentmesh.store import get_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(coordinator, "_service", None)
    with TestClient(coordinator.app) as c:
        yield c


def _invoke(client, payload):
    resp = client.post("/invocations", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_ping(client):
    assert client.get("/ping").json()["status"] in ("Healthy", "HealthyBusy")


def test_full_flow_over_http(client):
    opened = _invoke(client, {"action": "simulate", "scenario": "bad-deploy"})
    incident_id = opened["incident"]["id"]
    [approval] = _invoke(client, {"action": "list_approvals"})["approvals"]

    decided = _invoke(
        client, {"action": "decide_approval", "approval_id": approval["id"], "approve": True, "approver": "amir"}
    )

    assert decided["incident"]["status"] == "resolved"
    shown = _invoke(client, {"action": "get_incident", "incident_id": incident_id})
    assert shown["incident"]["postmortem"]
    assert [i["id"] for i in _invoke(client, {"action": "list_incidents"})["incidents"]] == [incident_id]
    assert all(s["healthy"] for s in _invoke(client, {"action": "health"})["services"])


def test_async_mode_returns_immediately_and_finishes_in_background(client):
    opened = _invoke(client, {"action": "simulate", "scenario": "traffic-spike", "async": True})
    assert opened["status"] == "processing"
    incident_id = opened["incident"]["id"]
    deadline = time.time() + 30
    while get_store().get_incident(incident_id)["status"] != "awaiting_approval":
        assert time.time() < deadline, "background coordinator did not finish"
        time.sleep(0.1)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"action": "explode"}, "unknown action"),
        ({"action": "open_incident"}, "missing field(s): title"),
        ({"action": "simulate", "scenario": "meteor"}, "Unknown scenario"),
        ({"action": "decide_approval", "approval_id": "apr-x", "approver": "a"}, "approve"),
        ({"action": "decide_approval", "approval_id": "apr-x", "approve": True, "approver": "a"}, "Unknown approval"),
        ({"action": "get_incident", "incident_id": "inc-nope"}, "Unknown incident"),
    ],
)
def test_bad_requests_return_errors(client, payload, error):
    assert error in _invoke(client, payload)["error"]


def test_non_object_payload(client):
    resp = client.post("/invocations", json=["not", "an", "object"])
    assert "error" in resp.json()


def test_remote_cli_payload_shape(monkeypatch):
    """The CLI's --remote mode calls InvokeAgentRuntime with a JSON payload and a long session id."""
    from agentmesh import cli

    captured = {}

    class FakeBody:
        def read(self):
            return b'{"incidents": []}'

    class FakeClient:
        def invoke_agent_runtime(self, **kwargs):
            captured.update(kwargs)
            return {"response": FakeBody()}

    monkeypatch.setattr("boto3.client", lambda *a, **k: FakeClient())
    arn = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/agentmesh_coordinator-abc"
    assert cli.main(["--remote", arn, "incidents"]) == 0
    assert captured["agentRuntimeArn"] == arn
    assert len(captured["runtimeSessionId"]) >= 33
    assert httpx.Response(200, content=captured["payload"]).json() == {"action": "list_incidents"}


def test_cli_url_mode_talks_to_a_running_coordinator(client, monkeypatch, capsys):
    """`agentmesh --url ...` (used with `agentmesh up`) sends payloads to /invocations."""
    from agentmesh import cli

    def post(url, json, timeout):
        assert url == "http://127.0.0.1:8080/invocations"
        return client.post("/invocations", json=json)

    monkeypatch.setattr("httpx.post", post)
    assert cli.main(["--url", "http://127.0.0.1:8080/", "simulate", "bad-deploy"]) == 0
    assert "Waiting for human approval" in capsys.readouterr().out
    assert cli.main(["--url", "http://127.0.0.1:8080", "approvals"]) == 0
    assert "rollback_deployment" in capsys.readouterr().out
