"""Render the dashboard HTML.

Uses Jinja2 with autoescaping on, matching the agent's renderer. Report
headlines and category names originate from CloudTrail data and model output,
so escaping is not optional here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

import charts
from anonymize import DEMO_NOTICE, sample_report_url
from history import DayRecord

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def status_kind(record: DayRecord) -> str:
    """Map a day's outcome to a badge kind."""
    if record.status == "FAILED":
        return "high"
    if record.fallback_used or record.partial:
        return "med"
    return "low"


def ai_kind(record: DayRecord) -> str:
    if record.ai_status == "SUCCESS":
        return "low"
    if record.ai_status in {"FAILED", "FAILED_VALIDATION"}:
        return "high"
    return "med"


@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["status_kind"] = status_kind
    env.filters["ai_kind"] = ai_kind
    return env


def render_dashboard(
    records: list[DayRecord],
    summary: dict,
    token: str = "",
    demo: bool = False,
) -> str:
    """Build the history page.

    ``records`` arrives newest-first; charts read left-to-right oldest-first.
    In demo mode the caller has already anonymized ``records``.
    """
    chronological = list(reversed(records))
    labels = [r.date for r in chronological]

    volume_chart = charts.grouped_bars(
        labels,
        [
            ("Events kept", charts.ACCENT, [r.events_included for r in chronological]),
            ("Failures", charts.DANGER, [r.failed_events for r in chronological]),
            ("Episodes", charts.OK, [r.episode_count for r in chronological]),
        ],
        title="Daily activity",
    )

    category_chart = charts.stacked_bars(
        labels,
        [r.episodes_by_category for r in chronological],
        charts.CATEGORY_COLORS,
        title="Episodes by category",
    )

    signal = charts.signal_bar(
        summary.get("eventsCollected", 0), summary.get("eventsIncluded", 0)
    )

    # Link tokens travel in the query string so a plain <a> works without JS.
    token_query = f"?token={quote(token)}" if token else ""

    return _env().get_template("dashboard.html.j2").render(
        records=records,
        summary=summary,
        volume_chart=volume_chart,
        category_chart=category_chart,
        signal_chart=signal,
        token_query=token_query,
        has_history=bool(records),
        demo=demo,
        demo_notice=DEMO_NOTICE if demo else "",
        sample_report=sample_report_url() if demo else "",
    )
