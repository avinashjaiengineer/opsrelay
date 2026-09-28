"""The event-boundary Lambda (infra/lambda/handler.py), with the coordinator call faked."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "handler", Path(__file__).parent.parent / "infra" / "lambda" / "handler.py"
)
handler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler)


@pytest.fixture
def calls(monkeypatch):
    sent = []
    replies = {}

    def fake_invoke(payload):
        sent.append(payload)
        key = payload.get("job_id") or payload.get("message") or payload["action"]
        return replies.get(key, {"results": [{"outcome": "created"}]})

    monkeypatch.setattr(handler, "invoke", fake_invoke)
    return sent, replies


def _records(*bodies):
    return {"Records": [{"messageId": f"m{i}", "body": b} for i, b in enumerate(bodies)]}


def test_intake_reports_only_failed_messages(calls, monkeypatch):
    sent, replies = calls
    monkeypatch.setenv("MODE", "intake")
    replies["garbage"] = {"error": "unrecognized alert format"}
    out = handler.handler(_records('{"detail-type": "CloudWatch Alarm State Change"}', "garbage"), None)
    assert [p["action"] for p in sent] == ["ingest_alert", "ingest_alert"]
    assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_jobs_retry_while_another_worker_holds_the_lease(calls, monkeypatch):
    sent, replies = calls
    monkeypatch.setenv("MODE", "jobs")
    replies["job-a"] = {"job_id": "job-a", "status": "done"}
    replies["job-b"] = {"job_id": "job-b", "status": "in_progress"}
    out = handler.handler(_records(json.dumps({"job_id": "job-a"}), json.dumps({"job_id": "job-b"})), None)
    assert [p["job_id"] for p in sent] == ["job-a", "job-b"]
    assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_recover_mode_calls_recover(calls, monkeypatch):
    sent, _ = calls
    monkeypatch.setenv("MODE", "recover")
    handler.handler({}, None)
    assert sent == [{"action": "recover"}]


def test_invoke_signs_the_request(monkeypatch):
    monkeypatch.setenv(
        "COORDINATOR_ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/opsrelay_coordinator-x"
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured["url"], captured["headers"] = request.full_url, dict(request.header_items())
        return FakeResponse()

    monkeypatch.setattr(handler.urllib.request, "urlopen", fake_urlopen)
    assert handler.invoke({"action": "recover"}) == {"ok": True}
    assert captured["url"].startswith(
        "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore"
    )
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/bedrock-agentcore/aws4_request" in headers["authorization"]
    assert len(headers["x-amzn-bedrock-agentcore-runtime-session-id"]) >= 33
