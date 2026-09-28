"""The coordinator talking to specialists over real A2A HTTP servers, as on AgentCore."""

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from opsrelay.config import SPECIALISTS
from opsrelay.remote import a2a_invoker
from opsrelay.runtime.specialist import build_app
from opsrelay.service import IncidentService
from opsrelay.store import get_store


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def specialists():
    """Start the four specialist A2A servers on local ports; yield {role: url}."""
    servers, urls = [], {}
    for role in SPECIALISTS:
        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(
            uvicorn.Config(build_app(role, url + "/"), host="127.0.0.1", port=port, log_level="warning")
        )
        threading.Thread(target=server.run, daemon=True).start()
        servers.append(server)
        urls[role] = url
    deadline = time.time() + 20
    for url in urls.values():
        while True:
            try:
                if httpx.get(url + "/ping").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.time() > deadline:
                raise RuntimeError(f"A2A server {url} did not start")
            time.sleep(0.1)
    yield urls
    for server in servers:
        server.should_exit = True


def test_agent_card_advertises_the_role_skill(specialists):
    card = httpx.get(specialists["diagnostics"] + "/.well-known/agent-card.json").json()
    assert card["name"] == "opsrelay-diagnostics"
    assert card["url"] == specialists["diagnostics"] + "/"
    assert [s["id"] for s in card["skills"]] == ["incident-diagnostics"]
    assert card["capabilities"]["streaming"] is True


def test_incident_resolved_across_a2a_agents(specialists):
    store = get_store()  # the servers and the coordinator share the same store, like DynamoDB on AWS
    invokers = {role: a2a_invoker(role, url, timeout=60) for role, url in specialists.items()}
    service = IncidentService(store=store, invokers=invokers)

    opened = service.simulate("memory-leak")
    assert opened["incident"]["status"] == "awaiting_approval"
    [approval] = service.list_approvals()

    decided = service.decide_approval(approval["id"], approve=True, approver="sre@example.com")

    assert decided["incident"]["status"] == "resolved"
    events = service.get_incident(opened["incident"]["id"])["events"]
    responders = {e["actor"] for e in events if e["kind"] == "a2a.response"}
    assert responders == set(SPECIALISTS)
    # Tool calls were made (and audited) inside the specialist servers, not the coordinator.
    assert any(e["actor"] == "diagnostics" and e["kind"] == "tool.call" for e in events)


def test_unreachable_specialist_is_reported_not_fatal(specialists):
    store = get_store()
    invokers = {role: a2a_invoker(role, url, timeout=5) for role, url in specialists.items()}
    invokers["diagnostics"] = a2a_invoker("diagnostics", f"http://127.0.0.1:{_free_port()}", timeout=2)
    service = IncidentService(store=store, invokers=invokers)

    opened = service.simulate("bad-deploy")

    events = service.get_incident(opened["incident"]["id"])["events"]
    assert any(e["kind"] == "a2a.error" and "diagnostics" in e["message"] for e in events)
    assert opened["incident"]["status"] == "escalated"
