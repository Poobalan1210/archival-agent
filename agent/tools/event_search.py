"""Strands tool: search normalized events from the current run (plan section 15)."""

from __future__ import annotations

from strands import tool

from agent.models.enums import EventResult
from agent.tools.context import get_analysis_context, record_tool_call, serialize_event


def _matches(
    event,
    service: str | None,
    operation: str | None,
    result: str | None,
    resource: str | None,
) -> bool:
    if service and service.lower() not in event.event_source.lower():
        return False
    if operation and operation.lower() not in event.event_name.lower():
        return False
    if result:
        wanted = result.strip().upper()
        actual = "FAILURE" if event.is_failure() else "SUCCESS"
        if wanted not in {"SUCCESS", "FAILURE"} or wanted != actual:
            return False
    if resource:
        names = " ".join(event.resource_names()).lower()
        if resource.lower() not in names:
            return False
    return True


@tool
def search_events(
    service: str | None = None,
    operation: str | None = None,
    result: str | None = None,
    resource: str | None = None,
) -> dict:
    """Search the current run's normalized events by simple filters.

    Use this to confirm or expand on an episode: to check whether a resource was
    touched more than once, whether an actor had other activity, or whether a
    failure recurred. All filters are optional and combine with AND. String
    filters are case-insensitive substring matches.

    Only events from the current report window are searchable. Results are
    capped and already redacted. Cite the returned event IDs as evidence.

    Args:
        service: Match part of the event source, e.g. "iam", "ec2",
            "cloudformation".
        operation: Match part of the event name, e.g. "CreateStack",
            "AuthorizeSecurityGroupIngress".
        result: Restrict to "SUCCESS" or "FAILURE".
        resource: Match part of a resource name or ARN.

    Returns:
        A dict with the total number of matches and up to ``max_records`` events.
    """
    context = get_analysis_context()

    matched = [
        event
        for event in context.events
        if _matches(event, service, operation, result, resource)
    ]
    limited = matched[: context.max_records]

    arguments = {
        "service": service,
        "operation": operation,
        "result": result,
        "resource": resource,
    }
    record_tool_call("search_events", arguments, len(limited))
    return {
        "totalMatched": len(matched),
        "returned": len(limited),
        "truncated": len(matched) > len(limited),
        "events": [serialize_event(event) for event in limited],
    }
