"""Retrieval: embeddings, runbook search, and runbook citations in the workflow."""

import pytest

from opsrelay import knowledge, runbooks
from opsrelay.service import IncidentService


def test_lexical_embeddings_are_normalized_and_similar_texts_score_higher():
    _, a = knowledge.embed("pods OOMKilled, heap usage high", "lexical")
    _, b = knowledge.embed("OOMKilled pods and high heap usage", "lexical")
    _, c = knowledge.embed("flash sale traffic, CPU saturated", "lexical")
    assert abs(sum(x * x for x in a) - 1) < 1e-9
    assert knowledge.cosine(a, b) > knowledge.cosine(a, c)


def test_bedrock_embedder_falls_back_to_lexical(monkeypatch):
    def boom(*_args):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(knowledge, "_titan", boom)
    kind, vector = knowledge.embed("anything", "bedrock")
    assert kind == "lexical" and len(vector) == knowledge.LEXICAL_DIMENSIONS


def test_builtin_runbooks_load():
    books = runbooks.all_runbooks()
    assert {"RB-001", "RB-002", "RB-003"} <= set(books)
    assert books["RB-002"].actions == ("restart_service", "rollback_deployment")
    assert books["RB-006"].actions == ()


@pytest.mark.parametrize(
    ("query", "category", "service", "expected"),
    [
        ("pods OOMKilled, heap near the limit, login latency high", "memory-leak", "auth-service", "RB-002"),
        ("p99 latency high, CPU 96%, flash sale traffic", "saturation", "inventory-service", "RB-003"),
        ("5xx errors after the release", "bad-deploy", "web-frontend", "RB-001"),
        ("checkout failing after a deploy", "bad-deploy", "checkout-api", "RB-004"),
        ("connection pool exhausted, replication lag", "dependency", "payments-db", "RB-006"),
    ],
)
def test_search_finds_the_right_runbook(query, category, service, expected):
    [(top, _score), *_] = runbooks.search(query, category=category, service=service)
    assert top.id == expected


def test_search_without_hints_uses_meaning():
    [(top, _score), *_] = runbooks.search("OOMKilled pods memory exhaustion restart")
    assert top.id == "RB-002"


def test_your_own_runbooks_override_builtins(tmp_path, monkeypatch):
    (tmp_path / "mine.md").write_text(
        "---\nid: RB-002\ntitle: Our memory runbook\ncategories: [memory-leak]\n"
        "actions: [rollback_deployment]\n---\nRoll back.\n",
        encoding="utf-8",
    )
    (tmp_path / "new.md").write_text(
        "---\nid: RB-100\ntitle: Disk full\ncategories: [dependency]\n---\nFree space.\n", encoding="utf-8"
    )
    monkeypatch.setenv("OPSRELAY_RUNBOOK_DIR", str(tmp_path))
    from opsrelay.config import get_settings

    get_settings.cache_clear()
    assert runbooks.get("RB-002").actions == ("rollback_deployment",)
    assert runbooks.get("RB-100").title == "Disk full"
    with pytest.raises(ValueError, match="front matter"):
        runbooks.parse("no front matter")


def test_proposals_cite_the_runbook_they_follow(store, env):
    service = IncidentService(store, env)
    incident = service.simulate("memory-leak")["incident"]
    [approval] = store.list_approvals(incident_id=incident["id"])
    assert approval["action"] == "restart_service" and approval["runbook_id"] == "RB-002"
    assert "follows runbook RB-002" in approval["policy"]["reasons"]


def test_policy_test_can_check_a_runbook(store, env):
    decision = IncidentService(store, env).test_policy(
        "scale_service", "inventory-service", {"replicas": 4}, runbook_id="RB-002"
    )
    assert decision["decision"] == "APPROVAL_REQUIRED"
    assert "RB-002 recommends restart_service, rollback_deployment; not scale_service" in decision["reasons"][-1]
