"""Group filtered events into operational episodes (plan section 13).

Each event is assigned to the first correlation key available in the configured
priority order (CloudFormation stack, CodePipeline execution, request ID,
principal session, resource ARN, then event-source + time window). Events that
share a key form one :class:`Episode`.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict

from agent.config import Config
from agent.models.enums import EpisodeCategory
from agent.models.episode import Episode
from agent.models.event import NormalizedEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Correlation-key strategies
# ---------------------------------------------------------------------------


def _stack_id(event: NormalizedEvent) -> str | None:
    if event.event_source != "cloudformation.amazonaws.com":
        return None
    for resource in event.resources:
        if resource.arn and ":stack/" in resource.arn:
            return resource.arn
    for key in ("stackId", "stackName"):
        if event.request.get(key):
            return str(event.request[key])
    if event.resources and event.resources[0].name:
        return event.resources[0].name
    return None


def _pipeline_execution_id(event: NormalizedEvent) -> str | None:
    if "codepipeline" not in event.event_source:
        return None
    for key in ("pipelineExecutionId", "executionId", "pipelineName"):
        if event.request.get(key):
            return str(event.request[key])
    return None


def _time_bucket(event: NormalizedEvent, window_seconds: int) -> int:
    return int(event.event_time.timestamp() // max(window_seconds, 1))


def _correlation_key(
    event: NormalizedEvent, priority: list[str], window_seconds: int
) -> str:
    for strategy in priority:
        if strategy == "cloudformation_stack_id":
            value = _stack_id(event)
            if value:
                return f"cfn:{value}"
        elif strategy == "codepipeline_execution_id":
            value = _pipeline_execution_id(event)
            if value:
                return f"pipeline:{value}"
        elif strategy == "request_id":
            if event.request_id:
                return f"req:{event.request_id}"
        elif strategy == "principal_session":
            if event.session_key:
                bucket = _time_bucket(event, window_seconds)
                return f"session:{event.session_key}:{bucket}"
        elif strategy == "resource_arn":
            names = event.resource_names()
            if names:
                bucket = _time_bucket(event, window_seconds)
                return f"resource:{names[0]}:{bucket}"
        elif strategy == "source_time_window":
            bucket = _time_bucket(event, window_seconds)
            return f"source:{event.event_source}:{bucket}"
    # Guaranteed fallback.
    return f"source:{event.event_source}:{_time_bucket(event, window_seconds)}"


# ---------------------------------------------------------------------------
# Episode assembly
# ---------------------------------------------------------------------------


def _mechanism(key: str, events: list[NormalizedEvent]) -> str:
    if key.startswith("cfn:"):
        return "CloudFormation"
    if key.startswith("pipeline:"):
        return "CodePipeline"
    sources = Counter(e.event_source for e in events)
    top_source = sources.most_common(1)[0][0]
    service = top_source.split(".")[0]
    return service.upper() if len(service) <= 3 else service.capitalize()


def _episode_category(domain: str, failed: bool) -> EpisodeCategory:
    if failed and domain == "DEPLOYMENT":
        return EpisodeCategory.FAILED_DEPLOYMENT
    if failed and domain in {"OTHER", ""}:
        return EpisodeCategory.FAILED_OPERATION
    try:
        return EpisodeCategory(domain)
    except ValueError:
        return EpisodeCategory.OTHER


def _summarize(
    mechanism: str,
    category: EpisodeCategory,
    events: list[NormalizedEvent],
    resources: list[str],
    failed: bool,
) -> str:
    resource_str = ", ".join(resources[:5]) if resources else "no named resources"
    operations = ", ".join(sorted({e.event_name for e in events})[:5])
    outcome = "FAILED" if failed else "succeeded"
    base = (
        f"{len(events)} {mechanism} event(s) [{operations}] affecting "
        f"{resource_str}; {outcome}."
    )
    if failed:
        first_error = next((e.error for e in events if e.error), None)
        if first_error:
            base += f" First error: {first_error}"
    return base


def group_episodes(events: list[NormalizedEvent], config: Config) -> list[Episode]:
    """Group filtered events into ordered episodes."""
    rules = config.event_categories
    priority: list[str] = rules.get("grouping_priority", ["source_time_window"])
    window_seconds: int = int(rules.get("time_window_seconds", 300))

    groups: dict[str, list[NormalizedEvent]] = defaultdict(list)
    for event in events:
        key = _correlation_key(event, priority, window_seconds)
        groups[key].append(event)

    episodes: list[Episode] = []
    for key, group_events in groups.items():
        group_events.sort(key=lambda e: e.event_time)
        failed = any(e.is_failure() for e in group_events)

        actor_counts = Counter(
            e.principal.name for e in group_events if e.principal.name
        )
        actor = actor_counts.most_common(1)[0][0] if actor_counts else None

        category_counts = Counter(e.category or "OTHER" for e in group_events)
        dominant = category_counts.most_common(1)[0][0]
        episode_category = _episode_category(dominant, failed)

        regions = sorted({e.region for e in group_events})
        resources = sorted({name for e in group_events for name in e.resource_names()})
        mechanism = _mechanism(key, group_events)

        episodes.append(
            Episode(
                episodeId="pending",
                category=episode_category,
                startTime=group_events[0].event_time,
                endTime=group_events[-1].event_time,
                actor=actor,
                mechanism=mechanism,
                regions=regions,
                resources=resources,
                status="FAILED" if failed else "SUCCESS",
                eventIds=[e.event_id for e in group_events],
                deterministicSummary=_summarize(
                    mechanism, episode_category, group_events, resources, failed
                ),
                groupingKey=key,
            )
        )

    # Stable, human-friendly ordering: newest activity first is useful, but the
    # plan renders a chronological timeline, so order by start time ascending.
    episodes.sort(key=lambda ep: ep.start_time)
    for index, episode in enumerate(episodes, start=1):
        episode.episode_id = f"episode-{index:03d}"

    logger.info("Grouped %d events into %d episodes", len(events), len(episodes))
    return episodes
