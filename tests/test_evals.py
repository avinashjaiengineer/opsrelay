"""Agent evaluation and incident replay."""

import pytest

from opsrelay import evals, offline
from opsrelay.cli import main


def test_offline_agents_pass_every_builtin_case():
    report = evals.evaluate(runs=1, model="offline")
    s = report["summary"]
    assert s["cases"] == len(evals.load_cases()) >= 6 and s["errors"] == 0
    assert (s["triage"], s["diagnosis"], s["action"], s["escalation"], s["runbook_cited"]) == (1, 1, 1, 1, 1)
    assert s["unsafe"] == 0 and s["contract_violations"] == 0
    injection = next(r for r in report["results"] if r["case"] == "prompt-injection")
    assert [p["action"] for p in injection["proposals"]] == ["restart_service"]


def test_scoring_flags_wrong_and_unsafe_actions():
    case = {"id": "c", "expect": {"service": "a", "actions": ["restart_service"], "must_not": ["scale_service"]}}
    out = {
        "service": "a",
        "severity": "SEV2",
        "category": "memory-leak",
        "status": "escalated",
        "proposals": [{"action": "scale_service", "runbook_id": None, "decision": "ALLOW", "status": "executed"}],
    }
    checks = evals.score(case, out)["checks"]
    assert checks["action"] is False and checks["unsafe"] is True and checks["runbook_cited"] is False
    assert checks["diagnosis"] is None and checks["escalation"] is None  # not expected, not scored


def test_a_wrong_agent_is_caught(monkeypatch):
    monkeypatch.setitem(offline.PREFERRED_ACTION, "memory-leak", "rollback_deployment")
    report = evals.evaluate(model="offline", only=["memory-leak"])
    [result] = report["results"]
    assert result["checks"]["action"] is False and report["summary"]["action"] == 0
    assert result["status"] == "escalated"  # the simulated person rejected it


def test_custom_cases_are_validated(tmp_path):
    bad = tmp_path / "cases.yaml"
    bad.write_text("cases:\n  - id: x\n    scenario: nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown scenario"):
        evals.load_cases(str(bad))


def test_replay_shows_what_changed(service, monkeypatch):
    incident = service.simulate("memory-leak")["incident"]
    [approval] = service.list_approvals()
    service.decide_approval(approval["id"], approve=True, approver="jane")

    same = evals.replay(service.store, incident["id"], model="offline")
    assert same["differences"] == [] and same["after"]["status"] == "resolved"

    # The agent now proposes something the person never approved: replay rejects it, and says so.
    monkeypatch.setitem(offline.PREFERRED_ACTION, "memory-leak", "rollback_deployment")
    changed = evals.replay(service.store, incident["id"], model="offline")
    assert "status: resolved -> escalated" in changed["differences"]
    assert any(
        d.startswith("proposals: ['restart_service (RB-002, APPROVAL_REQUIRED)']") for d in changed["differences"]
    )
    assert len(service.store.list_incidents()) == 1  # the sandbox never touches the real store


def test_replay_needs_a_scenario(service):
    incident = service.open_incident("Something odd", "no scenario behind it", run=False)["incident"]
    with pytest.raises(ValueError, match="not raised by a scenario"):
        evals.replay(service.store, incident["id"])


def test_cli_eval_saves_and_gates(capsys):
    assert main(["eval", "--case", "traffic-spike", "--save", "--fail-under", "1"]) == 0
    assert "traffic-spike" in capsys.readouterr().out
    from opsrelay.store import get_store

    assert get_store().list_records("eval")
