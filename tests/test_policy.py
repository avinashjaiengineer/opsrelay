"""The policy engine is pure: a proposal and facts in, a decision with reasons out."""

import pytest

from opsrelay.policy import Facts, load_policy
from opsrelay.schemas import RemediationProposal

ALL_ACTIONS = ("rollback_deployment", "restart_service", "scale_service", "flush_cache")


def _facts(tier=2, confidence=0.95, proposals=0, versions=("1.0", "1.1"), runbook_actions=ALL_ACTIONS):
    return Facts(
        runbook_actions=runbook_actions,
        service_tier=tier,
        service_max_replicas=8,
        deployed_versions=versions,
        diagnosis_confidence=confidence,
        proposals_so_far=proposals,
    )


def _decide(action, facts=None, risk="low", runbook_id="RB-TEST", **parameters):
    proposal = RemediationProposal(
        incident_id="inc-0123456789",
        action=action,
        service="svc",
        parameters=parameters,
        risk=risk,
        rollback_plan="undo",
        rationale="why",
        runbook_id=runbook_id,
    )
    return load_policy().evaluate(proposal, facts or _facts())


def test_low_risk_scale_is_allowed_without_a_person():
    decision = _decide("scale_service", replicas=4)
    assert (decision.decision, decision.risk, decision.requires_human) == ("ALLOW", "low", False)


def test_tier_one_raises_risk_and_requires_a_person():
    decision = _decide("scale_service", _facts(tier=1), replicas=4)
    assert (decision.decision, decision.risk) == ("APPROVAL_REQUIRED", "medium")


def test_tier_one_bump_never_reaches_critical():
    assert _decide("rollback_deployment", _facts(tier=1)).risk == "high"


def test_agent_can_raise_risk_but_not_lower_it():
    assert _decide("scale_service", risk="high", replicas=4).risk == "high"
    assert _decide("rollback_deployment", risk="low").risk == "high"


@pytest.mark.parametrize(
    ("action", "parameters", "reason"),
    [
        ("delete_database", {}, "never allowed"),
        ("drop_everything", {}, "not a known action"),
        ("scale_service", {"replicas": 50}, "from 1 to 8"),  # the service's own limit is lower than policy's
        ("scale_service", {}, "requires the parameter 'replicas'"),
        ("restart_service", {"replicas": 2}, "takes no parameter 'replicas'"),
        ("rollback_deployment", {"target_version": "0.9"}, "previous version (1.0)"),
    ],
)
def test_denials_explain_why(action, parameters, reason):
    decision = _decide(action, **parameters)
    assert decision.decision == "DENY" and not decision.allowed
    assert any(reason in r for r in decision.reasons), decision.reasons


def test_confidence_thresholds():
    assert _decide("scale_service", _facts(confidence=0.95), replicas=4).decision == "ALLOW"
    middling = _decide("scale_service", _facts(confidence=0.8), replicas=4)
    assert middling.decision == "APPROVAL_REQUIRED"
    assert any("needs human review" in r for r in middling.reasons)
    assert _decide("scale_service", _facts(confidence=0.5), replicas=4).decision == "DENY"
    assert _decide("scale_service", _facts(confidence=None), replicas=4).decision == "DENY"


def test_proposal_limit_per_incident():
    assert _decide("scale_service", _facts(proposals=3), replicas=4).decision == "DENY"


def test_custom_policy_file(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("version: 7\nactions:\n  restart_service: {risk: low, requires_approval: false}\n")
    policy = load_policy(str(path))
    proposal = RemediationProposal(
        incident_id="inc-0123456789",
        action="restart_service",
        service="svc",
        risk="low",
        rollback_plan="n/a",
        rationale="why",
    )
    decision = policy.evaluate(proposal, _facts())
    assert decision.decision == "ALLOW" and decision.policy_version.startswith("v7-")
    with pytest.raises(ValueError, match="risk must be one of"):
        bad = tmp_path / "bad.yaml"
        bad.write_text("actions:\n  x: {risk: extreme}\n")
        load_policy(str(bad))


def test_runbook_citation():
    followed = _decide("scale_service", _facts(runbook_actions=("scale_service",)), runbook_id="RB-003", replicas=4)
    assert followed.decision == "ALLOW" and "follows runbook RB-003" in followed.reasons

    uncited = _decide("scale_service", runbook_id=None, replicas=4)
    assert uncited.decision == "APPROVAL_REQUIRED" and "no runbook cited" in uncited.reasons[-1]

    missing = _decide("scale_service", _facts(runbook_actions=None), runbook_id="RB-999", replicas=4)
    assert missing.decision == "APPROVAL_REQUIRED" and "RB-999 does not exist" in missing.reasons[-1]

    off = _decide(
        "scale_service",
        _facts(runbook_actions=("restart_service", "rollback_deployment")),
        runbook_id="RB-002",
        replicas=4,
    )
    assert off.decision == "APPROVAL_REQUIRED"
    assert "RB-002 recommends restart_service, rollback_deployment; not scale_service" in off.reasons[-1]
