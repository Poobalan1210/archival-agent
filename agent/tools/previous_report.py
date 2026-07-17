"""Strands tool: fetch a previous structured report from S3 (plan section 15).

In the MVP this is context only. Do not claim that an earlier issue was resolved
based solely on its absence today; note it as an observation instead.
"""

from __future__ import annotations

from strands import tool

from agent.tools.context import get_analysis_context, record_tool_call


@tool
def get_previous_report(days_ago: int = 1) -> dict:
    """Return a previous day's structured report from S3, if one exists.

    Use this only for lightweight continuity, for example to note that a failed
    deployment also appeared yesterday. Treat it as historical context, not as
    proof that anything was fixed or caused.

    Args:
        days_ago: How many days back to look, relative to the current report
            date. Defaults to 1 (the previous day). Must be >= 1.

    Returns:
        A dict with ``available`` and, when present, the previous ``report``.
        When no report is found, ``available`` is false with a short message.
    """
    context = get_analysis_context()
    days_ago = max(1, int(days_ago))

    if context.previous_report_loader is None:
        record_tool_call("get_previous_report", {"days_ago": days_ago}, 0)
        return {
            "available": False,
            "message": "Previous-report lookup is not configured for this run.",
        }

    try:
        report = context.previous_report_loader(days_ago)
    except Exception:  # noqa: BLE001 - tool errors must not abort the agent
        record_tool_call("get_previous_report", {"days_ago": days_ago}, 0)
        return {
            "available": False,
            "message": "Failed to load a previous report.",
        }

    found = report is not None
    record_tool_call("get_previous_report", {"days_ago": days_ago}, 1 if found else 0)
    if not found:
        return {
            "available": False,
            "message": f"No report found for {days_ago} day(s) ago.",
        }
    return {"available": True, "daysAgo": days_ago, "report": report}
