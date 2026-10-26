"""Per-run context shared with the read-only Strands tools.

Strands ``@tool`` functions are plain module-level callables, so they need a way
to reach the current run's episodes and events. We store an
:class:`AnalysisContext` in a :class:`~contextvars.ContextVar` for the duration
of one analysis and expose small accessors the tools use.

All data placed here is already normalized and redacted. Tools must never
expose more than ``max_records`` items and must treat event content as data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from agent.models.episode import Episode
from agent.models.event import NormalizedEvent

logger = logging.getLogger(__name__)

# Fields that are internal correlation aids, not evidence for the model.
_EVIDENCE_EXCLUDE = {"request_id", "session_key"}


@dataclass
class ToolCall:
    name: str
    arguments: dict
    result_count: int | None = None


@dataclass
class AnalysisContext:
    """Everything the read-only tools may access during one analysis."""

    episodes_by_id: dict[str, Episode]
    events_by_id: dict[str, NormalizedEvent]
    events: list[NormalizedEvent]
    max_records: int = 50
    previous_report_loader: Callable[[int], dict | None] | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # diagnose_access_denied results, keyed by (principal, action, resource).
    access_diagnoses: dict[tuple, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        episodes: list[Episode],
        events: list[NormalizedEvent],
        max_records: int = 50,
        previous_report_loader: Callable[[int], dict | None] | None = None,
    ) -> "AnalysisContext":
        return cls(
            episodes_by_id={ep.episode_id: ep for ep in episodes},
            events_by_id={ev.event_id: ev for ev in events},
            events=list(events),
            max_records=max_records,
            previous_report_loader=previous_report_loader,
        )


_CURRENT: ContextVar[AnalysisContext | None] = ContextVar("analysis_context", default=None)


def set_analysis_context(context: AnalysisContext) -> None:
    _CURRENT.set(context)


def clear_analysis_context() -> None:
    _CURRENT.set(None)


def get_analysis_context() -> AnalysisContext:
    context = _CURRENT.get()
    if context is None:
        raise RuntimeError(
            "No analysis context is active. Tools may only run inside an analysis."
        )
    return context


def record_tool_call(name: str, arguments: dict, result_count: int | None = None) -> None:
    context = _CURRENT.get()
    if context is not None:
        context.tool_calls.append(
            ToolCall(name=name, arguments=arguments, result_count=result_count)
        )
    logger.info("tool=%s args=%s results=%s | tool invoked", name, arguments, result_count)


def serialize_event(event: NormalizedEvent) -> dict:
    """Compact, redacted evidence representation of one event."""
    return event.model_dump(by_alias=True, mode="json", exclude=_EVIDENCE_EXCLUDE)
