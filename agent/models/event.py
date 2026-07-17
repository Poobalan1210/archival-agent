"""Normalized CloudTrail event model (plan section 11).

The normalizer parses the raw ``CloudTrailEvent`` JSON string and produces a
compact, redacted, evidence-friendly representation. Every field originating
from CloudTrail is untrusted data and must never be treated as instructions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.models.enums import EventResult


class Principal(BaseModel):
    """Who performed the action."""

    model_config = ConfigDict(populate_by_name=True)

    type: str | None = None
    name: str | None = None
    invoked_by: str | None = Field(default=None, alias="invokedBy")
    account_id: str | None = Field(default=None, alias="accountId")
    principal_id: str | None = Field(default=None, alias="principalId")


class SourceInfo(BaseModel):
    """Origin metadata for the call."""

    model_config = ConfigDict(populate_by_name=True)

    user_agent: str | None = Field(default=None, alias="userAgent")
    source_ip: str | None = Field(default=None, alias="sourceIp")
    source_ip_type: str | None = Field(default=None, alias="sourceIpType")


class ResourceRef(BaseModel):
    """A resource referenced by the event."""

    type: str | None = None
    name: str | None = None
    arn: str | None = None


class NormalizedEvent(BaseModel):
    """Compact, redacted representation of one CloudTrail management event."""

    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(alias="eventId")
    event_time: datetime = Field(alias="eventTime")
    region: str
    event_source: str = Field(alias="eventSource")
    event_name: str = Field(alias="eventName")
    event_category: str = Field(default="Management", alias="eventCategory")
    read_only: bool = Field(default=False, alias="readOnly")
    result: EventResult = EventResult.SUCCESS
    principal: Principal = Field(default_factory=Principal)
    source: SourceInfo = Field(default_factory=SourceInfo)
    resources: list[ResourceRef] = Field(default_factory=list)
    request: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    error_code: str | None = Field(default=None, alias="errorCode")

    # Internal correlation fields (used by the grouper; not primary evidence).
    request_id: str | None = Field(default=None, alias="requestId")
    session_key: str | None = Field(default=None, alias="sessionKey")

    # Populated by the classifier/filter stages (not from CloudTrail).
    category: str | None = None

    def resource_names(self) -> list[str]:
        """All non-empty resource names referenced by this event."""
        names: list[str] = []
        for resource in self.resources:
            if resource.name:
                names.append(resource.name)
            elif resource.arn:
                names.append(resource.arn)
        return names

    def is_failure(self) -> bool:
        return self.result == EventResult.FAILURE or bool(self.error_code)

    def summary_line(self) -> str:
        """One-line, human-readable description used in deterministic output."""
        actor = self.principal.name or self.principal.type or "unknown principal"
        outcome = "FAILED" if self.is_failure() else "ok"
        target = ", ".join(self.resource_names()) or "-"
        return (
            f"{self.event_time.isoformat()} [{self.region}] "
            f"{self.event_source.split('.')[0]}:{self.event_name} "
            f"by {actor} -> {outcome} ({target})"
        )
