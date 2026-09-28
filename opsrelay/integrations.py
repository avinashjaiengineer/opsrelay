"""Slack, Microsoft Teams, PagerDuty and Jira, through a durable outbox.

After every coordinator run, `sync` works out what the incident's state calls for:

    a proposal awaits a person   -> Slack (with Approve / Reject buttons), Teams (links to the dashboard)
    SEV1, or escalated           -> PagerDuty incident (dedup key = incident id), Jira ticket
    escalated                    -> Slack, Teams
    resolved                     -> Slack, Teams; PagerDuty resolve and a Jira comment with the
                                    postmortem, if a page or ticket was opened

Each notification is created once, as a `notification` record whose id names the incident, the
event and the channel, so a rerun never sends it twice. A `notify` job (opsrelay.jobs) delivers it:
retried on failure, dead-lettered (without escalating the incident) if it keeps failing.

Slack button clicks arrive either at /integrations/slack/actions (the request's Slack signature is
verified) or, with OPSRELAY_SLACK_APP_TOKEN, over a Socket Mode connection OpsRelay opens to Slack
(authenticated by the app token; no public URL needed). Either way the Slack user is mapped to an
OpsRelay user (OPSRELAY_SLACK_USERS, or `slack_id` in the dev users
file), and the decision is authorized by role exactly like one made in the dashboard.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx
import yaml

from . import secrets
from .config import get_settings
from .lifecycle import Status
from .store import Record, Store, now_iso
from .store.base import new_record

log = logging.getLogger(__name__)
KIND = "notification"
CHAT = ("slack", "teams")
TIMEOUT = 10.0


class DeliveryError(RuntimeError):
    pass


def enabled() -> set[str]:
    s = get_settings()
    on = set()
    if s.slack_bot_token and s.slack_channel:
        on.add("slack")
    if s.teams_webhook_url:
        on.add("teams")
    if s.pagerduty_routing_key:
        on.add("pagerduty")
    if s.jira_url and s.jira_email and s.jira_api_token and s.jira_project:
        on.add("jira")
    return on


def notification_id(incident_id: str, event: str, channel: str) -> str:
    return f"ntf-{incident_id}-{event}-{channel}"


def _sent(store: Store, incident_id: str, event: str, channel: str) -> bool:
    return store.get_record(KIND, notification_id(incident_id, event, channel)) is not None


def plan(store: Store, incident: Record, channels: set[str]) -> list[tuple[str, str]]:
    """(event, channel) pairs the incident's current state calls for, oldest concern first."""
    iid, status = incident["id"], incident["status"]
    out: list[tuple[str, str]] = []
    for approval in store.list_approvals(status="pending", incident_id=iid):
        out += [(f"approval-{approval['id']}", c) for c in CHAT]
    serious = incident.get("severity") == "SEV1" or status == Status.ESCALATED
    if serious and status != Status.RESOLVED:
        out += [("page", "pagerduty"), ("ticket", "jira")]
    if status == Status.ESCALATED:
        out += [("escalated", c) for c in CHAT]
    if status == Status.RESOLVED:
        out += [("resolved", c) for c in CHAT]
        if _sent(store, iid, "page", "pagerduty"):
            out.append(("resolved", "pagerduty"))
        if _sent(store, iid, "ticket", "jira"):
            out.append(("resolved", "jira"))
    return [(e, c) for e, c in out if c in channels]


def sync(store: Store, incident_id: str) -> list[str]:
    """Create and queue the notifications this incident needs now. Returns their ids."""
    channels = enabled()
    incident = store.get_incident(incident_id)
    if not channels or incident is None:
        return []
    from . import jobs  # jobs import integrations for delivery

    queued = []
    for event, channel in plan(store, incident, channels):
        nid = notification_id(incident_id, event, channel)
        record = new_record(KIND, nid, "pending", incident_id=incident_id, event=event, channel=channel, result=None)
        if store.put_record(record):  # False: already created by an earlier sync
            jobs.enqueue(store, incident_id, "", action="notify", notification_id=nid)
            queued.append(nid)
    return queued


def sync_quietly(store: Store, incident_id: str) -> None:
    try:
        sync(store, incident_id)
    except Exception:  # noqa: BLE001 - notifications must never break incident handling
        log.exception("could not queue notifications for %s", incident_id)


def deliver(store: Store, nid: str) -> Record:
    """Send one notification (a `notify` job). Raises DeliveryError to be retried."""
    note = store.get_record(KIND, nid)
    if note is None:
        raise DeliveryError(f"unknown notification {nid}")
    if note["status"] == "sent":
        return note
    incident = store.get_incident(note["incident_id"])
    if incident is None:
        raise DeliveryError(f"unknown incident {note['incident_id']}")
    sender = SENDERS[note["channel"]]
    result = sender(store, incident, note["event"])
    moved = store.move_record(KIND, nid, note["rev"], {"status": "sent", "result": result, "sent_at": now_iso()})
    store.record(
        incident["id"], "platform", "notification.sent", f"{note['channel']}: {note['event']}", {"notification_id": nid}
    )
    return moved or note


# --- Message content ----------------------------------------------------------------------------


def _link(incident: Record) -> str | None:
    base = get_settings().public_url.rstrip("/")
    return f"{base}/?incident={incident['id']}" if base else None


def _headline(incident: Record) -> str:
    sev = incident.get("severity") or "unrated"
    return f"[{sev}] {incident['title']} ({incident.get('service') or 'service unknown'})"


def _pending(store: Store, event: str) -> Record | None:
    return store.get_approval(event.removeprefix("approval-")) if event.startswith("approval-") else None


def _describe(store: Store, incident: Record, event: str) -> tuple[str, list[tuple[str, str]]]:
    """(one-line text, facts) for a chat message."""
    facts = [("Incident", incident["id"]), ("Status", incident["status"])]
    if incident.get("root_cause"):
        facts.append(("Root cause", incident["root_cause"]))
    approval = _pending(store, event)
    if approval:
        facts += [
            ("Proposed", f"{approval['action']} on {approval['service']} ({approval['risk']} risk)"),
            ("Runbook", approval.get("runbook_id") or "none cited"),
            ("Policy", "; ".join(approval["policy"]["reasons"])),
        ]
        return f"Approval needed: {_headline(incident)}", facts
    if event == "escalated":
        facts.append(("Why", incident.get("escalation_reason") or ""))
        return f"Escalated to a person: {_headline(incident)}", facts
    if event == "resolved":
        facts.append(("Outcome", (incident.get("verification") or {}).get("summary", "")))
        return f"Resolved: {_headline(incident)}", facts
    return _headline(incident), facts


# --- Senders --------------------------------------------------------------------------------------


def _check(resp: httpx.Response, what: str) -> Any:
    if resp.status_code >= 300:
        raise DeliveryError(f"{what}: HTTP {resp.status_code} {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        return {}


def send_slack(store: Store, incident: Record, event: str) -> Record:
    s = get_settings()
    text, facts = _describe(store, incident, event)
    blocks: list[Record] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"[:1900]} for k, v in facts[:10]]},
    ]
    elements: list[Record] = []
    approval = _pending(store, event)
    if approval and approval["status"] == "pending":
        elements += [
            {
                "type": "button",
                "action_id": "opsrelay_approve",
                "style": "primary",
                "text": {"type": "plain_text", "text": "Approve"},
                "value": approval["id"],
            },
            {
                "type": "button",
                "action_id": "opsrelay_reject",
                "style": "danger",
                "text": {"type": "plain_text", "text": "Reject"},
                "value": approval["id"],
            },
        ]
    if _link(incident):
        elements.append(
            {"type": "button", "text": {"type": "plain_text", "text": "Open in OpsRelay"}, "url": _link(incident)}
        )
    if elements:
        blocks.append({"type": "actions", "elements": elements})
    resp = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {secrets.resolve(s.slack_bot_token)}"},
        json={"channel": s.slack_channel, "text": text, "blocks": blocks},
        timeout=TIMEOUT,
    )
    body = _check(resp, "Slack")
    if not body.get("ok"):
        raise DeliveryError(f"Slack: {body.get('error', 'not ok')}")
    return {"channel": body.get("channel"), "ts": body.get("ts")}


def send_teams(store: Store, incident: Record, event: str) -> Record:
    text, facts = _describe(store, incident, event)
    card: Record = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": text, "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]},
        ],
    }
    if _link(incident):
        title = "Review and decide" if _pending(store, event) else "Open in OpsRelay"
        card["actions"] = [{"type": "Action.OpenUrl", "title": title, "url": _link(incident)}]
    message = {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }
    resp = httpx.post(secrets.resolve(get_settings().teams_webhook_url), json=message, timeout=TIMEOUT)
    _check(resp, "Teams")
    return {"status": resp.status_code}


PD_SEVERITY = {"SEV1": "critical", "SEV2": "error", "SEV3": "warning", "SEV4": "info"}


def send_pagerduty(store: Store, incident: Record, event: str) -> Record:
    body: Record = {
        "routing_key": secrets.resolve(get_settings().pagerduty_routing_key),
        "event_action": "resolve" if event == "resolved" else "trigger",
        "dedup_key": incident["id"],
    }
    if body["event_action"] == "trigger":
        body["payload"] = {
            "summary": _headline(incident)[:1024],
            "source": incident.get("service") or "opsrelay",
            "severity": PD_SEVERITY.get(incident.get("severity") or "", "error"),
            "custom_details": {
                "status": incident["status"],
                "root_cause": incident.get("root_cause"),
                "escalation_reason": incident.get("escalation_reason"),
            },
        }
        if _link(incident):
            body["links"] = [{"href": _link(incident), "text": "OpsRelay incident"}]
    resp = httpx.post("https://events.pagerduty.com/v2/enqueue", json=body, timeout=TIMEOUT)
    result = _check(resp, "PagerDuty")
    return {"dedup_key": result.get("dedup_key", incident["id"]), "action": body["event_action"]}


def _adf(*blocks: Record) -> Record:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def _para(text: str) -> Record:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def send_jira(store: Store, incident: Record, event: str) -> Record:
    s = get_settings()
    auth = (s.jira_email, secrets.resolve(s.jira_api_token))
    base = s.jira_url.rstrip("/")
    if event == "resolved":
        ticket = store.get_record(KIND, notification_id(incident["id"], "ticket", "jira"))
        key = ((ticket or {}).get("result") or {}).get("key")
        if not key:
            raise DeliveryError("the Jira ticket for this incident was not created (yet)")
        from .postmortem import render

        pm = render(store, incident["id"])["markdown"]
        comment = _adf(
            _para("Resolved by OpsRelay. Postmortem:"),
            {"type": "codeBlock", "attrs": {"language": "markdown"}, "content": [{"type": "text", "text": pm[:30000]}]},
        )
        resp = httpx.post(f"{base}/rest/api/3/issue/{key}/comment", auth=auth, json={"body": comment}, timeout=TIMEOUT)
        _check(resp, "Jira")
        return {"key": key, "commented": True}
    text, facts = _describe(store, incident, event)
    lines = [f"{k}: {v}" for k, v in facts] + ([f"Dashboard: {_link(incident)}"] if _link(incident) else [])
    fields = {
        "project": {"key": s.jira_project},
        "issuetype": {"name": s.jira_issue_type},
        "summary": _headline(incident)[:250],
        "description": _adf(*(_para(line) for line in lines)),
        "labels": ["opsrelay", (incident.get("severity") or "unrated").lower()],
    }
    resp = httpx.post(f"{base}/rest/api/3/issue", auth=auth, json={"fields": fields}, timeout=TIMEOUT)
    created = _check(resp, "Jira")
    return {"key": created.get("key"), "url": f"{base}/browse/{created.get('key')}"}


SENDERS = {"slack": send_slack, "teams": send_teams, "pagerduty": send_pagerduty, "jira": send_jira}


# --- Slack interactivity ------------------------------------------------------------------------


def verify_slack(body: bytes, timestamp: str, signature: str, now: float | None = None) -> bool:
    """Slack's v0 request signature: HMAC-SHA256 over "v0:{timestamp}:{body}", within 5 minutes."""
    secret = secrets.resolve(get_settings().slack_signing_secret)
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    expected = "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _yaml_source(source: str) -> Record:
    from pathlib import Path

    if not source:
        return {}
    text = secrets.resolve(source) if secrets.is_reference(source) else Path(source).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def slack_principal(slack_user_id: str):  # noqa: ANN201 - auth.Principal | None
    """The OpsRelay user behind a Slack user: from OPSRELAY_SLACK_USERS, or a `slack_id` in the dev
    users file. None if the Slack user isn't mapped (they may not decide anything)."""
    from .auth import ROLES, Principal

    s = get_settings()
    for source in (s.slack_users, s.dev_users):
        for user in _yaml_source(source).get("users") or []:
            if user.get("slack_id") == slack_user_id:
                roles = frozenset(r for r in user.get("roles") or [] if r in ROLES)
                return Principal(f"slack:{slack_user_id}", str(user["name"]), roles, "slack", True)
    return None


def handle_slack_action(payload: Record, decide) -> str:  # noqa: ANN001
    """Apply a button click. `decide(approval_id, approve, principal) -> Record` does the decision.
    Returns the text to show in Slack in place of the buttons."""
    from .rbac import Forbidden

    action = (payload.get("actions") or [{}])[0]
    if action.get("action_id") not in ("opsrelay_approve", "opsrelay_reject"):
        return "Unknown action."
    user = (payload.get("user") or {}).get("id", "")
    principal = slack_principal(user)
    if principal is None:
        return f"<@{user}> is not mapped to an OpsRelay user, so this decision was not recorded."
    approve = action["action_id"] == "opsrelay_approve"
    try:
        approval = decide(action.get("value", ""), approve, principal)
    except Forbidden as e:
        return f"Not recorded: {e}"
    except (KeyError, ValueError) as e:
        return f"Not recorded: {str(e).strip(chr(39))}"
    verb = "approved" if approve else "rejected"
    return f"{approval['action']} on {approval['service']} {verb} by {principal.name} (via Slack)."


def respond_in_slack(response_url: str, text: str) -> None:
    if not response_url.startswith("https://hooks.slack.com/"):
        log.warning("ignoring a Slack response_url outside hooks.slack.com")
        return
    try:
        httpx.post(response_url, json={"replace_original": True, "text": text}, timeout=TIMEOUT)
    except httpx.HTTPError:
        log.exception("could not update the Slack message")


class SlackSocket:
    """Socket Mode: receives Slack interactions over a WebSocket this process opens (slack_sdk)."""

    def __init__(self, app_token: str, on_click):  # noqa: ANN001
        self.app_token = app_token
        self.on_click = on_click
        self.client = None

    def start(self) -> "SlackSocket":
        from slack_sdk.socket_mode import SocketModeClient

        self.client = SocketModeClient(app_token=self.app_token, logger=log)
        self.client.socket_mode_request_listeners.append(self.handle)
        self.client.connect()
        log.info("Slack Socket Mode connected")
        return self

    def handle(self, client, request) -> None:  # noqa: ANN001
        from slack_sdk.socket_mode.response import SocketModeResponse

        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))  # ack within 3 s
        payload = request.payload if isinstance(request.payload, dict) else {}
        if request.type == "interactive" and payload.get("type") == "block_actions":
            import threading

            threading.Thread(target=self.on_click, args=(payload,), name="slack-action", daemon=True).start()


def parse_slack_form(body: bytes) -> Record:
    from urllib.parse import parse_qs

    return json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
