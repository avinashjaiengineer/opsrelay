"""The policy engine: decides whether a proposed remediation may run, and who must approve it.

    RemediationProposal -> evaluate() -> PolicyDecision(ALLOW | APPROVAL_REQUIRED | DENY)

Rules live in policies.yaml (or the file named by OPSRELAY_POLICY_FILE). The engine is pure: it
sees the proposal and facts about the service and diagnosis, and returns a decision with the
reasons, so every decision can be explained, tested and replayed.
"""

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from .config import get_settings
from .schemas import RISK_ORDER, PolicyDecision, RemediationProposal, Risk


@dataclass(frozen=True)
class Facts:
    """What the engine knows besides the proposal."""

    service_tier: int
    service_max_replicas: int
    deployed_versions: tuple[str, ...]  # oldest first; the last one is running
    diagnosis_confidence: float | None
    proposals_so_far: int


@dataclass(frozen=True)
class Policy:
    doc: dict[str, Any]
    version: str

    @property
    def actions(self) -> dict[str, dict[str, Any]]:
        return self.doc.get("actions", {})

    def evaluate(self, proposal: RemediationProposal, facts: Facts) -> PolicyDecision:
        defaults = self.doc.get("defaults", {})
        confidence = self.doc.get("confidence", {})
        deny: list[str] = []
        notes: list[str] = []
        rule = self.actions.get(proposal.action)

        if rule is None:
            return self._decide(
                "DENY", "critical", [f"{proposal.action} is not a known action; unknown actions are denied"]
            )
        if rule.get("allowed", True) is False:
            return self._decide("DENY", rule.get("risk", "critical"), [f"{proposal.action} is never allowed by policy"])

        risk: Risk = rule.get("risk", "high")
        if defaults.get("tier1_raises_risk") and facts.service_tier == 1 and RISK_ORDER.index(risk) < 2:
            risk = RISK_ORDER[RISK_ORDER.index(risk) + 1]
            notes.append(f"{proposal.service} is tier 1, so the risk is raised to {risk}")
        if RISK_ORDER.index(proposal.risk) > RISK_ORDER.index(risk):
            risk = proposal.risk
            notes.append(f"the agent assessed the risk as {proposal.risk}")

        allowed = set(rule.get("parameters", [])) | set(rule.get("required_parameters", []))
        for name in proposal.parameters:
            if name not in allowed:
                deny.append(f"{proposal.action} takes no parameter '{name}'")
        for name in rule.get("required_parameters", []):
            if name not in proposal.parameters:
                deny.append(f"{proposal.action} requires the parameter '{name}'")

        if "replicas" in proposal.parameters:
            limit = min(int(rule.get("max_replicas", facts.service_max_replicas)), facts.service_max_replicas)
            replicas = proposal.parameters["replicas"]
            if not isinstance(replicas, int) or not 1 <= replicas <= limit:
                deny.append(f"replicas must be an integer from 1 to {limit}")
        if "target_version" in proposal.parameters:
            previous = facts.deployed_versions[-2] if len(facts.deployed_versions) >= 2 else None
            if proposal.parameters["target_version"] != previous:
                deny.append(f"target_version must be the previous version ({previous or 'none deployed'})")

        max_proposals = int(defaults.get("max_proposals_per_incident", 3))
        if facts.proposals_so_far >= max_proposals:
            deny.append(f"this incident already has {facts.proposals_so_far} proposals (limit {max_proposals})")

        needs_human = bool(rule.get("requires_approval", True))
        if needs_human:
            notes.insert(0, f"{proposal.action} always requires approval")
        if facts.diagnosis_confidence is None:
            deny.append("there is no diagnosis to act on")
        elif facts.diagnosis_confidence < float(confidence.get("escalate_below", 0.7)):
            deny.append(
                f"diagnosis confidence {facts.diagnosis_confidence:.2f} is below "
                f"{float(confidence.get('escalate_below', 0.7)):.2f}; escalate to a human"
            )
        elif facts.diagnosis_confidence < float(confidence.get("human_required_below", 0.9)):
            needs_human = True
            notes.append(f"diagnosis confidence {facts.diagnosis_confidence:.2f} needs human review")

        if deny:
            return self._decide("DENY", risk, deny)
        at_risk = defaults.get("human_required_at_risk", "medium")
        if RISK_ORDER.index(risk) >= RISK_ORDER.index(at_risk):
            needs_human = True
            notes.append(f"{risk} risk requires a person (threshold: {at_risk})")
        if needs_human:
            return self._decide("APPROVAL_REQUIRED", risk, notes)
        return self._decide("ALLOW", risk, [*notes, f"{risk} risk; policy allows it without approval"])

    def _decide(self, decision: str, risk: Risk, reasons: list[str]) -> PolicyDecision:
        return PolicyDecision(
            decision=decision,
            allowed=decision != "DENY",
            requires_human=decision == "APPROVAL_REQUIRED",
            risk=risk,
            reasons=reasons,
            policy_version=self.version,
        )


def parse_policy(text: str, label: str | None = None) -> Policy:
    """Validate policy YAML and build a Policy. `label` names a stored version (e.g. "v3")."""
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict) or not isinstance(doc.get("actions"), dict):
        raise ValueError("a policy must have an 'actions' mapping")
    for name, rule in doc["actions"].items():
        if not isinstance(rule, dict):
            raise ValueError(f"policy for {name} must be a mapping")
        if rule.get("risk", "high") not in RISK_ORDER:
            raise ValueError(f"policy for {name}: risk must be one of {RISK_ORDER}")
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    return Policy(doc=doc, version=f"{label or 'v' + str(doc.get('version', 1))}-{digest}")


def load_policy(path: str | None = None) -> Policy:
    text = Path(path).read_text(encoding="utf-8") if path else files("opsrelay").joinpath("policies.yaml").read_text()
    return parse_policy(text)


@lru_cache
def _file_policy() -> Policy:
    return load_policy(get_settings().policy_file or None)


@lru_cache(maxsize=16)
def _stored_policy(record_id: str, rev: int, text: str) -> Policy:  # noqa: ARG001 - rev keys the cache
    return parse_policy(text, label=record_id)


def get_policy(store: Any = None) -> Policy:
    """The policy in force: the active stored version (see opsrelay.policy_admin), or, if none has
    been activated, the file (OPSRELAY_POLICY_FILE, or the built-in policies.yaml)."""
    if store is not None:
        active = store.list_records("policy", status="active", limit=1)
        if active:
            return _stored_policy(active[0]["id"], active[0]["rev"], active[0]["text"])
    return _file_policy()


def _clear() -> None:
    _file_policy.cache_clear()
    _stored_policy.cache_clear()


get_policy.cache_clear = _clear  # type: ignore[attr-defined]
