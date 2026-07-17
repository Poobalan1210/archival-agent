"""Shared string enumerations used across Archival Agent models."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String enum whose ``str()`` value equals its member value.

    Provided explicitly for Python 3.11/3.12 compatibility and predictable
    JSON serialization.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class JobType(StrEnum):
    DAILY_ARCHIVAL = "DAILY_ARCHIVAL"


class RunStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"


class CollectionStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class AIStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    FALLBACK = "FALLBACK"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class EmailStatus(StrEnum):
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class EventResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class Priority(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EpisodeCategory(StrEnum):
    DEPLOYMENT = "DEPLOYMENT"
    FAILED_DEPLOYMENT = "FAILED_DEPLOYMENT"
    RESOURCE_LIFECYCLE = "RESOURCE_LIFECYCLE"
    IAM_POLICY = "IAM_POLICY"
    NETWORK = "NETWORK"
    SECURITY = "SECURITY"
    FAILED_OPERATION = "FAILED_OPERATION"
    OTHER = "OTHER"
