"""Structured AI report models (plan section 16).

These are the Pydantic models the Strands agent must return. The deterministic
validator (plan section 18) checks every instance against collected evidence
before the report is rendered. No numeric model confidence is accepted.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent.models.enums import Priority


class Recommendation(BaseModel):
    """A single evidence-backed, human-reviewed recommendation."""

    model_config = ConfigDict(populate_by_name=True)

    category: str
    priority: Priority
    title: str
    rationale: str
    suggested_next_step: str = Field(alias="suggestedNextStep")
    affected_resources: list[str] = Field(default_factory=list, alias="affectedResources")
    evidence_event_ids: list[str] = Field(default_factory=list, alias="evidenceEventIds")
    assumptions: list[str] = Field(default_factory=list)
    requires_human_review: bool = Field(default=True, alias="requiresHumanReview")


class EpisodeAnalysis(BaseModel):
    """The model's analysis of one episode."""

    model_config = ConfigDict(populate_by_name=True)

    episode_id: str = Field(alias="episodeId")
    facts: list[str] = Field(default_factory=list)
    interpretation: str | None = None
    priority: Priority = Priority.LOW
    evidence_event_ids: list[str] = Field(default_factory=list, alias="evidenceEventIds")


class ArchivalReport(BaseModel):
    """Top-level structured report produced by the agent (or the fallback)."""

    model_config = ConfigDict(populate_by_name=True)

    headline: str
    executive_summary: list[str] = Field(default_factory=list, alias="executiveSummary")
    episode_analyses: list[EpisodeAnalysis] = Field(
        default_factory=list, alias="episodeAnalyses"
    )
    recommendations: list[Recommendation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    def all_evidence_ids(self) -> set[str]:
        """Every event ID cited anywhere in the report."""
        ids: set[str] = set()
        for analysis in self.episode_analyses:
            ids.update(analysis.evidence_event_ids)
        for rec in self.recommendations:
            ids.update(rec.evidence_event_ids)
        return ids

    def referenced_episode_ids(self) -> set[str]:
        return {analysis.episode_id for analysis in self.episode_analyses}
