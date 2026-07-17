"""Deterministic event classification and filtering (plan section 12).

Filtering runs BEFORE Nova sees any data. It keeps meaningful management
activity (writes, failures, security/IAM/network/deployment changes) and drops
read-only noise, routine polling, and the agent's own activity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agent.config import Config
from agent.models.event import NormalizedEvent

logger = logging.getLogger(__name__)


@dataclass
class FilterStats:
    """Counts describing why events were kept or dropped."""

    total: int = 0
    included: int = 0
    dropped_read_only: int = 0
    dropped_noise_source: int = 0
    dropped_noise_name: int = 0
    dropped_self_noise: int = 0
    dropped_duplicate: int = 0
    kept_failure: int = 0
    kept_mutating: int = 0
    by_category: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_event(event: NormalizedEvent, categories_rules: dict) -> str:
    """Assign a domain category to an event; returns a category name or OTHER."""
    order: list[str] = categories_rules.get("category_order", [])
    categories: dict = categories_rules.get("categories", {})

    for name in order:
        rule = categories.get(name, {})
        if _category_matches(event, rule):
            return name
    return "OTHER"


def _category_matches(event: NormalizedEvent, rule: dict) -> bool:
    if event.event_source in rule.get("sources", []):
        return True
    name = event.event_name
    if any(token in name for token in rule.get("event_name_contains", [])):
        return True
    if any(name.startswith(prefix) for prefix in rule.get("event_name_prefixes", [])):
        return True
    return False


def is_read_only(event: NormalizedEvent, readonly_prefixes: list[str]) -> bool:
    """True for read operations. Explicit readOnly wins; else infer from name."""
    if event.read_only:
        return True
    return any(event.event_name.startswith(prefix) for prefix in readonly_prefixes)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _self_principal_substrings(config: Config) -> list[str]:
    """Substrings identifying the agent's own principals (self-noise)."""
    import os

    raw = os.environ.get("SELF_PRINCIPAL_SUBSTRINGS", "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    # Sensible defaults for this project's role naming.
    parts.extend(["archival-agent", "ArchivalAgent", "archival-dispatcher"])
    return parts


def _is_self_noise(event: NormalizedEvent, substrings: list[str]) -> bool:
    principal_name = (event.principal.name or "") + " " + (event.principal.type or "")
    invoked_by = event.principal.invoked_by or ""
    haystack = f"{principal_name} {invoked_by}".lower()
    return any(sub.lower() in haystack for sub in substrings)


def _dedupe_key(event: NormalizedEvent) -> tuple:
    return (
        event.event_source,
        event.event_name,
        event.principal.name,
        tuple(sorted(event.resource_names())),
        event.result,
    )


def filter_events(
    events: list[NormalizedEvent],
    config: Config,
    self_substrings: list[str] | None = None,
) -> tuple[list[NormalizedEvent], FilterStats]:
    """Classify and filter normalized events deterministically.

    Every event is assigned a ``category`` (in place). Returns the retained
    events plus :class:`FilterStats`.
    """
    rules = config.event_categories
    readonly_prefixes: list[str] = rules.get("readonly_prefixes", [])
    noise_sources = set(rules.get("noise_event_sources", []))
    noise_names = set(rules.get("noise_event_names", []))
    self_substrings = self_substrings or _self_principal_substrings(config)

    stats = FilterStats(total=len(events))
    included: list[NormalizedEvent] = []
    seen_keys: set[tuple] = set()

    for event in events:
        event.category = classify_event(event, rules)

        # 1. Never report the agent's own activity.
        if _is_self_noise(event, self_substrings):
            stats.dropped_self_noise += 1
            continue

        # 2. Failures are always interesting.
        if event.is_failure():
            included.append(event)
            stats.included += 1
            stats.kept_failure += 1
            stats.by_category[event.category] = stats.by_category.get(event.category, 0) + 1
            continue

        # 3. Drop known-noisy successful sources / names.
        if event.event_source in noise_sources:
            stats.dropped_noise_source += 1
            continue
        if event.event_name in noise_names:
            stats.dropped_noise_name += 1
            continue

        # 4. Drop successful read-only operations.
        if is_read_only(event, readonly_prefixes):
            stats.dropped_read_only += 1
            continue

        # 5. Collapse repeated identical successful mutations.
        key = _dedupe_key(event)
        if key in seen_keys:
            stats.dropped_duplicate += 1
            continue
        seen_keys.add(key)

        # 6. Remaining events are meaningful mutations.
        included.append(event)
        stats.included += 1
        stats.kept_mutating += 1
        stats.by_category[event.category] = stats.by_category.get(event.category, 0) + 1

    included.sort(key=lambda e: e.event_time)
    logger.info(
        "Filtered %d -> %d events (failures=%d, mutating=%d, dropped_readonly=%d)",
        stats.total,
        stats.included,
        stats.kept_failure,
        stats.kept_mutating,
        stats.dropped_read_only,
    )
    return included, stats
