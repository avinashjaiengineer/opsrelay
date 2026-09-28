"""Slack, Teams, PagerDuty and Jira through the notification outbox."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest

from opsrelay import integrations, jobs
from opsrelay.config import get_settings


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def post(self, url, json=None, headers=None, auth=None, timeout=None):  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers, "auth": auth})
        request = httpx.Request("POST", url)
        if any(part in url for part in self.fail):
            return httpx.Response(500, text="boom", request=request)
        if "slack.com" in url:
            return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.2"}, request=request)
        if "pagerduty" in url:
            return httpx.Response(202, json={"status": "success", "dedup_key": json["dedup_key"]}, request=request)
        if url.endswith("/rest/api/3/issue"):
            return httpx.Response(201, json={"key": "OPS-7"}, request=request)
        return httpx.Response(200, json={}, request=request)

    def to(self, part):
        return [c for c in self.calls if part in c["url"]]


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(integrations.httpx, "post", fake.post)
    return fake


@pytest.fixture
def configured(monkeypatch, tmp_path):
    users = tmp_path / "slack-users.yaml"
    users.write_text(
        "users:\n"
        "  - {slack_id: U_SRE, name: jane@corp, roles: [sre]}\n"
        "  - {slack_id: U_IC, name: ic@corp, roles: [incident_commander]}\n"
        "  - {slack_id: U_VIEW, name: viewer@corp, roles: [viewer]}\n",
        encoding="utf-8",
    )
    for key, value in {
        "PUBLIC_URL": "http://ops.example:8080",
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "C1",
        "SLACK_SIGNING_SECRET": "shh",
        "SLACK_USERS": str(users),
        "TEAMS_WEBHOOK_URL": "https://teams.example/webhook",
        "PAGERDUTY_ROUTING_KEY": "pd-key",
        "JIRA_URL": "https://corp.atlassian.net",
        "JIRA_EMAIL": "bot@corp",
        "JIRA_API_TOKEN": "jira-token",
        "JIRA_PROJECT": "OPS",
    }.items():
        monkeypatch.setenv(f"OPSRELAY_{key}", value)
    get_settings.cache_clear()


def _drain(service):
    worker = jobs.Worker(lambda: service)
    while worker.run_once():
        pass


def _notes(store):
    return {(n["event"].split("-")[0], n["channel"]): n for n in store.list_records("notification")}


def test_nothing_is_sent_when_nothing_is_configured(service, http):
    service.simulate("bad-deploy")
    _drain(service)
    assert http.calls == [] and service.store.list_records("notification") == []


def test_sev1_lifecycle_across_every_channel(service, http, configured):
    incident = service.simulate("bad-deploy")["incident"]
    assert (incident["severity"], incident["status"]) == ("SEV1", "awaiting_approval")
    _drain(service)
    notes = _notes(service.store)
    assert set(notes) == {("approval", "slack"), ("approval", "teams"), ("page", "pagerduty"), ("ticket", "jira")}
    assert all(n["status"] == "sent" for n in notes.values())

    [slack] = http.to("slack.com")
    buttons = slack["json"]["blocks"][-1]["elements"]
    [approval] = service.list_approvals()
    assert [b.get("action_id") for b in buttons[:2]] == ["opsrelay_approve", "opsrelay_reject"]
    assert buttons[0]["value"] == approval["id"]
    assert buttons[2]["url"] == f"http://ops.example:8080/?incident={incident['id']}"
    assert slack["headers"]["Authorization"] == "Bearer xoxb-test"
    [page] = http.to("pagerduty")
    assert (page["json"]["event_action"], page["json"]["dedup_key"]) == ("trigger", incident["id"])
    assert page["json"]["payload"]["severity"] == "critical"
    [ticket] = http.to("/rest/api/3/issue")
    assert ticket["json"]["fields"]["project"] == {"key": "OPS"} and ticket["auth"] == ("bot@corp", "jira-token")
    teams = http.to("teams.example")[0]["json"]["attachments"][0]["content"]
    assert teams["actions"][0]["title"] == "Review and decide"

    service.decide_approval(approval["id"], approve=True, approver="jane")
    _drain(service)
    notes = _notes(service.store)
    assert {("resolved", "slack"), ("resolved", "teams"), ("resolved", "pagerduty"), ("resolved", "jira")} <= set(notes)
    assert http.to("pagerduty")[-1]["json"] == {
        "routing_key": "pd-key",
        "event_action": "resolve",
        "dedup_key": incident["id"],
    }
    [comment] = http.to("/OPS-7/comment")
    assert "Postmortem" in json.dumps(comment["json"])

    before = len(http.calls)
    assert integrations.sync(service.store, incident["id"]) == []  # nothing is sent twice
    _drain(service)
    assert len(http.calls) == before


def test_escalation_pages_and_resolution_without_a_page_does_not(service, http, configured):
    service.simulate("traffic-spike")  # SEV3, scale is allowed by policy: resolved in one run
    _drain(service)
    assert {k for k in _notes(service.store)} == {("resolved", "slack"), ("resolved", "teams")}


def test_failed_deliveries_retry_then_dead_letter_without_escalating(service, http, configured, monkeypatch):
    monkeypatch.setenv("OPSRELAY_JOB_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    http.fail.add("teams.example")
    incident = service.simulate("memory-leak")["incident"]
    _drain(service)
    notes = _notes(service.store)
    assert notes[("approval", "slack")]["status"] == "sent"
    assert notes[("approval", "teams")]["status"] == "pending"
    assert len(http.to("teams.example")) == 2
    [letter] = service.store.list_dead_letters()
    assert letter["agent"] == "notify" and "HTTP 500" in letter["error"]
    assert service.store.get_incident(incident["id"])["status"] == "awaiting_approval"


def _sign(body: bytes, ts: str, secret: str = "shh") -> str:
    return "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()


def test_slack_signature(configured):
    ts = str(int(time.time()))
    body = b"payload=%7B%7D"
    assert integrations.verify_slack(body, ts, _sign(body, ts))
    assert not integrations.verify_slack(body, ts, _sign(body, ts, "wrong"))
    assert not integrations.verify_slack(body + b"x", ts, _sign(body, ts))
    old = str(int(time.time()) - 600)
    assert not integrations.verify_slack(body, old, _sign(body, old))


def _click(approval_id, user, action="opsrelay_approve"):
    return {
        "type": "block_actions",
        "user": {"id": user},
        "actions": [{"action_id": action, "value": approval_id}],
        "response_url": "https://hooks.slack.com/actions/T/1/x",
    }


def test_slack_buttons_decide_with_the_mapped_user_and_their_role(service, http, configured, monkeypatch):
    from opsrelay.runtime import coordinator

    monkeypatch.setattr(coordinator, "_service", service)
    monkeypatch.setattr(coordinator, "start_worker", lambda: None)
    service.simulate("bad-deploy")  # SEV1, high risk: only an incident commander may decide
    [approval] = service.list_approvals()

    coordinator._slack_click(_click(approval["id"], "U_NOBODY"))
    coordinator._slack_click(_click(approval["id"], "U_SRE"))
    assert service.store.get_approval(approval["id"])["status"] == "pending"
    coordinator._slack_click(_click(approval["id"], "U_IC", "opsrelay_reject"))
    decided = service.store.get_approval(approval["id"])
    assert (decided["status"], decided["decided_by"], decided["identity_verified"]) == ("rejected", "ic@corp", True)
    assert decided["note"] == "via Slack"

    replies = [c["json"]["text"] for c in http.to("hooks.slack.com")]
    assert "is not mapped to an OpsRelay user" in replies[0]
    assert "may not decide a high-risk action on a SEV1 incident" in replies[1]
    assert replies[2] == "rollback_deployment on checkout-api rejected by ic@corp (via Slack)."
    assert [j["action"] for j in service.store.list_records("job") if j["status"] == "queued"].count("decision") == 1


def test_slack_route_checks_the_signature(service, configured, monkeypatch):
    from starlette.testclient import TestClient

    from opsrelay.runtime import coordinator

    clicks = []
    monkeypatch.setattr(coordinator, "_slack_click", clicks.append)
    client = TestClient(coordinator.app)
    body = urlencode({"payload": json.dumps(_click("apr-x", "U_SRE"))}).encode()
    ts = str(int(time.time()))
    headers = {"content-type": "application/x-www-form-urlencoded", "x-slack-request-timestamp": ts}
    bad = client.post("/integrations/slack/actions", content=body, headers={**headers, "x-slack-signature": "v0=00"})
    assert bad.status_code == 401
    ok = client.post(
        "/integrations/slack/actions", content=body, headers={**headers, "x-slack-signature": _sign(body, ts)}
    )
    assert ok.status_code == 200
    time.sleep(0.2)
    assert clicks and clicks[0]["user"]["id"] == "U_SRE"


def test_secret_references_can_name_a_key(monkeypatch):
    from opsrelay import secrets

    stored = '{"opsrelay/slack-bot-token": "xoxb-1", "opsrelay/slack-signing-secret": " abc "}'
    monkeypatch.setattr(secrets, "_fetch", lambda secret_id, region: stored if secret_id == "opsrelay/" else "plain")
    assert secrets.resolve("secretsmanager:opsrelay/#opsrelay/slack-bot-token") == "xoxb-1"
    assert secrets.resolve("secretsmanager:opsrelay/#opsrelay/slack-signing-secret") == "abc"
    assert secrets.resolve("secretsmanager:other") == "plain"
    with pytest.raises(KeyError, match="no key 'missing'"):
        secrets.resolve("secretsmanager:opsrelay/#missing")


def test_socket_mode_acks_and_applies_button_clicks():
    import threading
    from types import SimpleNamespace

    clicked = threading.Event()
    seen = []

    def on_click(payload):
        seen.append(payload)
        clicked.set()

    acks = []
    client = SimpleNamespace(send_socket_mode_response=lambda response: acks.append(response.envelope_id))
    socket = integrations.SlackSocket("xapp-test", on_click)
    socket.handle(client, SimpleNamespace(type="interactive", envelope_id="e1", payload=_click("apr-1", "U_SRE")))
    socket.handle(client, SimpleNamespace(type="events_api", envelope_id="e2", payload={"type": "event_callback"}))
    assert acks == ["e1", "e2"]  # every envelope is acknowledged
    assert clicked.wait(2) and seen[0]["actions"][0]["value"] == "apr-1" and len(seen) == 1


def test_slack_approval_answers_at_once_and_a_job_runs_the_action(service, http, configured, monkeypatch):
    from opsrelay.runtime import coordinator

    monkeypatch.setattr(coordinator, "_service", service)
    monkeypatch.setattr(coordinator, "start_worker", lambda: None)
    incident = service.simulate("bad-deploy")["incident"]
    [approval] = service.list_approvals()
    coordinator._slack_click(_click(approval["id"], "U_IC"))
    # Recorded, not yet run: a real rollback takes minutes, longer than Slack or API Gateway wait.
    assert service.store.get_approval(approval["id"])["status"] == "approved"
    assert service.store.get_incident(incident["id"])["status"] == "remediating"
    assert "Running it now" in http.to("hooks.slack.com")[-1]["json"]["text"]
    _drain(service)  # the "decision" job: execute, verify, resolve
    assert service.store.get_approval(approval["id"])["status"] == "executed"
    assert service.store.get_incident(incident["id"])["status"] == "resolved"
