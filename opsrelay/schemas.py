"""Typed interfaces between the agents and the platform.

An agent's output never drives the workflow directly:

    LLM output -> pydantic validation (these models) -> policy engine -> workflow

Each specialist submits its result through one tool whose arguments are validated against a
model here; invalid output is rejected with the validation errors, so the agent can correct it.
Models forbid unknown fields.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

IncidentId = Annotated[str, StringConstraints(pattern=r"^inc-[0-9a-f]{10}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

Severity = Literal["SEV1", "SEV2", "SEV3", "SEV4"]
Risk = Literal["low", "medium", "high", "critical"]
RISK_ORDER: tuple[Risk, ...] = ("low", "medium", "high", "critical")
Category = Literal["bad-deploy", "memory-leak", "saturation", "dependency", "unknown"]
EvidenceSource = Literal["metrics", "logs", "deployment", "dependency", "cmdb", "alert"]
Audience = Literal["internal", "customers"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TriageResult(Contract):
    incident_id: IncidentId
    severity: Severity
    service: ShortText
    customer_impact: Text
    rationale: Text
    confidence: Confidence


class Evidence(Contract):
    source: EvidenceSource
    value: ShortText


class DiagnosisResult(Contract):
    incident_id: IncidentId
    root_cause: Text
    category: Category
    evidence: list[Evidence] = Field(min_length=1)
    confidence: Confidence
    affected_component: ShortText
    recommended_action: Text


class RemediationProposal(Contract):
    incident_id: IncidentId
    action: ShortText
    service: ShortText
    parameters: dict[str, int | str] = Field(default_factory=dict)
    risk: Risk  # the agent's own assessment; the policy engine may only raise it
    rollback_plan: Text
    rationale: Text
    runbook_id: ShortText | None = None  # the runbook this action follows (see opsrelay.runbooks)


class RemediationDecline(Contract):
    incident_id: IncidentId
    reason: Text


class VerificationResult(Contract):
    incident_id: IncidentId
    service: ShortText
    recovered: bool
    observations: list[Evidence] = Field(min_length=1)
    summary: Text


class StatusUpdate(Contract):
    incident_id: IncidentId
    audience: Audience
    message: Text


class Postmortem(Contract):
    incident_id: IncidentId
    summary: Text
    impact: Text
    timeline: list[ShortText] = Field(min_length=1)
    root_cause: Text
    resolution: Text
    contributing_factors: list[ShortText] = Field(default_factory=list)
    detection: Text
    action_items: list[ShortText] = Field(min_length=1)

    @field_validator("action_items")
    @classmethod
    def _unique(cls, items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))

    def markdown(self, title: str) -> str:
        def bullets(items: list[str], box: bool = False) -> str:
            return "\n".join(f"- {'[ ] ' if box else ''}{i}" for i in items) or "- None identified"

        return (
            f"# {self.incident_id} Postmortem: {title}\n\n"
            f"## Summary\n\n{self.summary}\n\n"
            f"## Impact\n\n{self.impact}\n\n"
            f"## Timeline\n\n{bullets(self.timeline)}\n\n"
            f"## Root cause\n\n{self.root_cause}\n\n"
            f"## Resolution\n\n{self.resolution}\n\n"
            f"## Contributing factors\n\n{bullets(self.contributing_factors)}\n\n"
            f"## Detection\n\n{self.detection}\n\n"
            f"## Action items\n\n{bullets(self.action_items, box=True)}\n"
        )


class PolicyDecision(Contract):
    decision: Literal["ALLOW", "APPROVAL_REQUIRED", "DENY"]
    allowed: bool
    requires_human: bool
    risk: Risk
    reasons: list[str]
    policy_version: str


def validation_errors(error: Exception) -> list[str]:
    """Readable pydantic errors for the agent: `field: problem`."""
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return [str(error)]
    return [f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in errors()]
