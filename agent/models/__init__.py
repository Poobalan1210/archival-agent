"""Pydantic data models for the Archival Agent."""

from agent.models.enums import (
    AIStatus,
    CollectionStatus,
    EmailStatus,
    EpisodeCategory,
    EventResult,
    JobType,
    Priority,
    RunStatus,
)
from agent.models.episode import Episode
from agent.models.event import NormalizedEvent, Principal, ResourceRef, SourceInfo
from agent.models.report import (
    ArchivalReport,
    EpisodeAnalysis,
    Recommendation,
)
from agent.models.request import (
    CollectionCoverage,
    DailyJobRequest,
    DailyJobResult,
    RegionFailure,
    RunContext,
    generate_run_id,
    generate_session_id,
)

__all__ = [
    # enums
    "AIStatus",
    "CollectionStatus",
    "EmailStatus",
    "EpisodeCategory",
    "EventResult",
    "JobType",
    "Priority",
    "RunStatus",
    # event
    "NormalizedEvent",
    "Principal",
    "ResourceRef",
    "SourceInfo",
    # episode
    "Episode",
    # report
    "ArchivalReport",
    "EpisodeAnalysis",
    "Recommendation",
    # request / result / context
    "CollectionCoverage",
    "DailyJobRequest",
    "DailyJobResult",
    "RegionFailure",
    "RunContext",
    "generate_run_id",
    "generate_session_id",
]
