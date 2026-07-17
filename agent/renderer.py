"""Render reports to HTML and JSON (plan section 19).

Jinja2 autoescaping is enabled, which is essential: event-derived strings are
untrusted and must be HTML-escaped to prevent injection into the report.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.config import Config, TEMPLATES_DIR
from agent.models.episode import Episode
from agent.models.event import NormalizedEvent
from agent.models.report import ArchivalReport
from agent.models.request import CollectionCoverage, RunContext

logger = logging.getLogger(__name__)

_SECTION_CATEGORIES = {
    "deployments": {"DEPLOYMENT"},
    "resource_lifecycle": {"RESOURCE_LIFECYCLE"},
    "iam_policy": {"IAM_POLICY"},
    "network": {"NETWORK"},
    "security": {"SECURITY"},
}


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def categorize_events(events: list[NormalizedEvent]) -> dict[str, list[NormalizedEvent]]:
    """Split events into the report's domain sections plus failures/timeline."""
    sections: dict[str, list[NormalizedEvent]] = {key: [] for key in _SECTION_CATEGORIES}
    sections["failed"] = []
    for event in events:
        for section, categories in _SECTION_CATEGORIES.items():
            if event.category in categories:
                sections[section].append(event)
        if event.is_failure():
            sections["failed"].append(event)
    return sections


def _totals(episodes: list[Episode], events: list[NormalizedEvent]) -> dict:
    by_category: dict[str, int] = {}
    for episode in episodes:
        key = str(episode.category)
        by_category[key] = by_category.get(key, 0) + 1
    return {
        "eventsIncluded": len(events),
        "failedEvents": sum(1 for e in events if e.is_failure()),
        "episodeCount": len(episodes),
        "episodesByCategory": by_category,
    }


def build_render_context(
    run_context: RunContext,
    coverage: CollectionCoverage,
    report: ArchivalReport,
    episodes: list[Episode],
    events: list[NormalizedEvent],
    agent_execution: dict[str, Any],
    config: Config,
    presigned_url: str | None = None,
) -> dict[str, Any]:
    """Assemble everything the templates need."""
    sections = categorize_events(events)
    timeline = sorted(events, key=lambda e: e.event_time)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": {
            "account_id": run_context.account_id,
            "report_date": run_context.report_date,
            "run_id": run_context.run_id,
            "session_id": run_context.session_id,
            "window_local": run_context.local_window_label(),
            "window_utc": run_context.utc_window_label(),
            "audit_regions": run_context.audit_regions,
        },
        "coverage": {
            "status": str(coverage.status),
            "requested_regions": coverage.requested_regions,
            "completed_regions": coverage.completed_regions,
            "failed_regions": [
                {"region": f.region, "error": f.error} for f in coverage.failed_regions
            ],
            "partial": bool(coverage.failed_regions),
        },
        "totals": _totals(episodes, events),
        "report": report,
        "episodes": episodes,
        "sections": sections,
        "timeline": timeline,
        "agent_execution": agent_execution,
        "presigned_url": presigned_url,
    }


def render_report_html(context: dict[str, Any]) -> str:
    return _env().get_template("report.html.j2").render(**context)


def render_email_html(context: dict[str, Any]) -> str:
    return _env().get_template("email.html.j2").render(**context)


def build_report_json(context: dict[str, Any]) -> dict:
    """Structured JSON mirror of the report for machine consumption."""
    report: ArchivalReport = context["report"]
    episodes: list[Episode] = context["episodes"]
    return {
        "generatedAt": context["generated_at"],
        "meta": context["meta"],
        "collection": context["coverage"],
        "totals": context["totals"],
        "agentExecution": context["agent_execution"],
        "report": report.model_dump(by_alias=True, mode="json"),
        "episodes": [ep.model_dump(by_alias=True, mode="json") for ep in episodes],
    }
