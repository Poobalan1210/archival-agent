"""Access-denied diagnosis model.

Produced deterministically by the ``diagnose_access_denied`` tool from the IAM
policy simulator. The model may cite a diagnosis by ID, but never writes its
contents: verdicts, policy statements and verification results come from code.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from agent.models.enums import StrEnum


class DiagnosisVerdict(StrEnum):
    MISSING_PERMISSION = "MISSING_PERMISSION"  # implicit deny; minimal fix verified by the simulator
    BLOCKED_BY_GUARDRAIL = "BLOCKED_BY_GUARDRAIL"  # implicit deny, but a boundary/SCP/resource policy also blocks
    EXPLICIT_DENY = "EXPLICIT_DENY"  # a Deny statement matched; granting would not help
    NOW_ALLOWED = "NOW_ALLOWED"  # current policies allow it: changed since, or denied by a policy type the simulator cannot see
    AWS_MANAGED_ROLE = "AWS_MANAGED_ROLE"  # service-linked role; not customer-modifiable
    CANNOT_DIAGNOSE = "CANNOT_DIAGNOSE"


class AccessDiagnosis(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    diagnosis_id: str = Field(alias="diagnosisId")
    verdict: DiagnosisVerdict
    principal_arn: str | None = Field(default=None, alias="principalArn")
    action: str | None = None
    resource: str | None = None
    explanation: str
    aws_error_reason: str | None = Field(default=None, alias="awsErrorReason")
    simulator_decision: str | None = Field(default=None, alias="simulatorDecision")
    deny_sources: list[str] = Field(default_factory=list, alias="denySources")
    proposed_policy: dict | None = Field(default=None, alias="proposedPolicy")
    fix_verified: bool = Field(default=False, alias="fixVerified")
    decision_with_fix: str | None = Field(default=None, alias="decisionWithFix")
    resource_policy_evaluated: bool = Field(default=False, alias="resourcePolicyEvaluated")
    occurrences: int = 0
    episode_ids: list[str] = Field(default_factory=list, alias="episodeIds")
    first_seen: datetime | None = Field(default=None, alias="firstSeen")
    last_seen: datetime | None = Field(default=None, alias="lastSeen")
    evidence_event_ids: list[str] = Field(default_factory=list, alias="evidenceEventIds")
    limitations: list[str] = Field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.verdict in {DiagnosisVerdict.MISSING_PERMISSION, DiagnosisVerdict.BLOCKED_BY_GUARDRAIL,
                                DiagnosisVerdict.EXPLICIT_DENY}
