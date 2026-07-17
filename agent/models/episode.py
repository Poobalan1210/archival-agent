"""Operational episode model (plan section 13).

An episode is a deterministic grouping of related events (e.g. one
CloudFormation stack update). The model produces this grouping; Nova only ever
sees the episode index plus, on request, the evidence for a single episode.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from agent.models.enums import EpisodeCategory


class Episode(BaseModel):
    """A correlated set of events forming one operational story."""

    model_config = ConfigDict(populate_by_name=True)

    episode_id: str = Field(alias="episodeId")
    category: EpisodeCategory = Field(default=EpisodeCategory.OTHER)
    start_time: datetime = Field(alias="startTime")
    end_time: datetime = Field(alias="endTime")
    actor: str | None = None
    mechanism: str | None = None
    regions: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    status: str = "UNKNOWN"
    event_ids: list[str] = Field(default_factory=list, alias="eventIds")
    deterministic_summary: str = Field(default="", alias="deterministicSummary")

    # Correlation key that produced the grouping (diagnostic only).
    grouping_key: str | None = Field(default=None, alias="groupingKey")

    @property
    def event_count(self) -> int:
        return len(self.event_ids)

    @property
    def failed(self) -> bool:
        return self.status.upper() in {"FAILED", "ROLLBACK", "ERROR"}

    def index_entry(self) -> dict:
        """Compact representation sent to the model as the episode index.

        Deliberately excludes raw event payloads; the agent must call
        ``get_episode_evidence`` to inspect details.
        """
        return {
            "episodeId": self.episode_id,
            "category": str(self.category),
            "startTime": self.start_time.isoformat(),
            "endTime": self.end_time.isoformat(),
            "actor": self.actor,
            "mechanism": self.mechanism,
            "regions": self.regions,
            "resources": self.resources,
            "status": self.status,
            "eventCount": self.event_count,
            "deterministicSummary": self.deterministic_summary,
        }
