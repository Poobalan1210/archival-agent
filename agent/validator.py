"""Deterministic validator and fallback report builder (plan section 18).

Every AI claim is checked against the collected evidence. Safety-critical
violations (destructive advice, permission expansion, unsupported quantitative
claims) discard the AI report entirely in favour of a deterministic fallback.
Lesser issues (unknown IDs, over-limit recommendations) are repaired in place
and noted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from agent.config import Config
from agent.models.enums import AIStatus, Priority
from agent.models.episode import Episode
from agent.models.event import NormalizedEvent
from agent.models.report import ArchivalReport, EpisodeAnalysis, Recommendation
from agent.models.request import CollectionCoverage

logger = logging.getLogger(__name__)


@dataclass
class ValidationOutcome:
    report: ArchivalReport
    ai_status: AIStatus
    fallback_used: bool
    violations: list[str] = field(default_factory=list)


def _contains_any(text: str, phrases: list[str]) -> str | None:
    lowered = text.lower()
    for phrase in phrases:
        if phrase.lower() in lowered:
            return phrase
    return None


# ---------------------------------------------------------------------------
# Numeric claims in model prose
#
# All counts in the report are computed deterministically. The model is only
# asked to explain episodes, but it will still sometimes write a count into a
# sentence, and a wrong count sitting next to the correct one destroys trust in
# the whole report. These helpers let the validator drop any prose that states a
# number the collected evidence does not support.
# ---------------------------------------------------------------------------

#: Standalone integers only. Skips values embedded in dates, times, event IDs,
#: ARNs and identifiers (anything adjacent to a word char, ``-`` or ``:``).
_INT_RE = re.compile(r"(?<![\w:.-])(\d+)(?![\w:.-])")


def _verifiable_numbers(
    episodes: list[Episode],
    events: list[NormalizedEvent],
    coverage: CollectionCoverage,
) -> set[int]:
    """Every integer the evidence actually supports."""
    failures = sum(1 for e in events if e.is_failure())
    by_category: dict[str, int] = {}
    for episode in episodes:
        key = str(episode.category)
        by_category[key] = by_category.get(key, 0) + 1

    allowed = {
        0,
        1,
        len(events),
        failures,
        len(events) - failures,
        len(episodes),
        len(coverage.requested_regions),
        len(coverage.completed_regions),
        len(coverage.failed_regions),
    }
    allowed.update(by_category.values())
    allowed.update(len(episode.event_ids) for episode in episodes)
    return allowed


def _unverified_numbers(text: str, allowed: set[int]) -> list[int]:
    """Integers in ``text`` that the evidence does not support."""
    found = (int(match) for match in _INT_RE.findall(text or ""))
    return sorted({n for n in found if n not in allowed})


def _deterministic_summary_lines(
    episodes: list[Episode],
    events: list[NormalizedEvent],
    coverage: CollectionCoverage,
) -> list[str]:
    """Summary bullets built entirely from computed counts."""
    failures = sum(1 for e in events if e.is_failure())
    by_category: dict[str, int] = {}
    for episode in episodes:
        key = str(episode.category)
        by_category[key] = by_category.get(key, 0) + 1

    lines = [
        f"{len(events)} meaningful events were grouped into {len(episodes)} episodes.",
        f"{failures} event(s) failed.",
        "Episodes by category: "
        + (", ".join(f"{k}={v}" for k, v in sorted(by_category.items())) or "none"),
    ]
    if coverage.failed_regions:
        lines.append(
            "Collection was PARTIAL; failed regions: "
            + ", ".join(f.region for f in coverage.failed_regions)
        )
    return lines


def _deterministic_headline(episodes: list[Episode], events: list[NormalizedEvent]) -> str:
    failures = sum(1 for e in events if e.is_failure())
    if not episodes:
        return "Deterministic summary: no significant activity detected."
    return (
        f"Deterministic summary: {len(episodes)} operational episode(s), "
        f"{failures} failed event(s)."
    )


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------


def _episode_priority(episode: Episode) -> Priority:
    if episode.failed:
        return Priority.HIGH
    if str(episode.category) in {"SECURITY", "IAM_POLICY", "NETWORK"}:
        return Priority.MEDIUM
    return Priority.LOW


def _fallback_recommendations(episodes: list[Episode]) -> list[Recommendation]:
    recommendations: list[Recommendation] = []

    for episode in episodes:
        if len(recommendations) >= 5:
            break
        if episode.failed:
            recommendations.append(
                Recommendation(
                    category="FAILED_DEPLOYMENT"
                    if "DEPLOYMENT" in str(episode.category)
                    else "OPERATIONAL_FOLLOW_UP",
                    priority=Priority.HIGH,
                    title=f"Review failed activity in {episode.episode_id}",
                    rationale=episode.deterministic_summary,
                    suggestedNextStep=(
                        "Open the first failed event and confirm whether the "
                        "operation needs to be retried or corrected."
                    ),
                    affectedResources=episode.resources,
                    evidenceEventIds=episode.event_ids,
                    assumptions=["Generated deterministically without AI analysis."],
                    requiresHumanReview=True,
                )
            )

    for episode in episodes:
        if len(recommendations) >= 5:
            break
        if not episode.failed and str(episode.category) in {"SECURITY", "IAM_POLICY", "NETWORK"}:
            recommendations.append(
                Recommendation(
                    category="SECURITY_REVIEW",
                    priority=Priority.MEDIUM,
                    title=f"Confirm intent of {episode.category} change in {episode.episode_id}",
                    rationale=episode.deterministic_summary,
                    suggestedNextStep="Confirm the change was intentional and expected.",
                    affectedResources=episode.resources,
                    evidenceEventIds=episode.event_ids,
                    assumptions=["Generated deterministically without AI analysis."],
                    requiresHumanReview=True,
                )
            )

    return recommendations[:5]


def build_fallback_report(
    episodes: list[Episode],
    events: list[NormalizedEvent],
    coverage: CollectionCoverage,
    reason: str,
) -> ArchivalReport:
    """Build a purely deterministic report from episode summaries and counts."""
    failures = sum(1 for e in events if e.is_failure())
    by_category: dict[str, int] = {}
    for episode in episodes:
        key = str(episode.category)
        by_category[key] = by_category.get(key, 0) + 1

    headline = _deterministic_headline(episodes, events)
    executive_summary = _deterministic_summary_lines(episodes, events, coverage)

    analyses = [
        EpisodeAnalysis(
            episodeId=episode.episode_id,
            facts=[episode.deterministic_summary],
            interpretation=None,
            priority=_episode_priority(episode),
            evidenceEventIds=episode.event_ids,
        )
        for episode in episodes
    ]

    return ArchivalReport(
        headline=headline,
        executiveSummary=executive_summary,
        episodeAnalyses=analyses,
        recommendations=_fallback_recommendations(episodes),
        limitations=[
            f"This is a deterministic report. Reason: {reason}",
            "No AI interpretation was applied to these events.",
        ],
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_or_fallback(
    agent_report: ArchivalReport | None,
    episodes: list[Episode],
    events: list[NormalizedEvent],
    coverage: CollectionCoverage,
    config: Config,
    ai_failed: bool = False,
) -> ValidationOutcome:
    """Validate the AI report, repairing minor issues or falling back.

    Returns the report to render along with the resulting AI status.
    """
    rules = config.recommendation_rules
    allowed_categories = set(rules.get("allowed_categories", []))
    max_recs = int(rules.get("max_recommendations", 5))
    destructive = rules.get("destructive_phrases", [])
    permission = rules.get("permission_expansion_phrases", [])
    quantitative = rules.get("quantitative_claim_phrases", [])

    if ai_failed or agent_report is None:
        report = build_fallback_report(
            episodes, events, coverage, reason="AI invocation failed."
        )
        return ValidationOutcome(
            report=report,
            ai_status=AIStatus.FALLBACK,
            fallback_used=True,
            violations=["AI invocation failed; deterministic fallback used."],
        )

    valid_episode_ids = {ep.episode_id for ep in episodes}
    valid_event_ids = {ev.event_id for ev in events}
    valid_resources = {
        name.lower() for ev in events for name in ev.resource_names()
    }

    violations: list[str] = []
    critical = False

    # -- Episode analyses --------------------------------------------------
    cleaned_analyses: list[EpisodeAnalysis] = []
    for analysis in agent_report.episode_analyses:
        if analysis.episode_id not in valid_episode_ids:
            violations.append(f"Dropped analysis for unknown episode {analysis.episode_id}.")
            continue
        kept_ids = [e for e in analysis.evidence_event_ids if e in valid_event_ids]
        if len(kept_ids) != len(analysis.evidence_event_ids):
            violations.append(f"Removed unknown evidence IDs from {analysis.episode_id}.")
        analysis.evidence_event_ids = kept_ids
        cleaned_analyses.append(analysis)

    # -- Recommendations ---------------------------------------------------
    cleaned_recs: list[Recommendation] = []
    for rec in agent_report.recommendations:
        text = " ".join([rec.title, rec.rationale, rec.suggested_next_step])
        # Destructive ADVICE lives in the actionable fields; the rationale may
        # legitimately describe a failed delete/terminate without advising one.
        action_text = " ".join([rec.title, rec.suggested_next_step])

        if rec.category not in allowed_categories:
            violations.append(f"Dropped recommendation with invalid category {rec.category}.")
            continue

        hit = _contains_any(action_text, destructive)
        if hit:
            violations.append(f"Destructive language detected: '{hit}'.")
            critical = True
            continue

        hit = _contains_any(text, permission)
        if hit:
            violations.append(f"Permission-expansion advice detected: '{hit}'.")
            critical = True
            continue

        hit = _contains_any(text, quantitative)
        if hit and not rec.evidence_event_ids:
            violations.append(f"Unsupported quantitative claim ('{hit}') without evidence.")
            critical = True
            continue

        rec.evidence_event_ids = [e for e in rec.evidence_event_ids if e in valid_event_ids]
        kept_resources = [
            r for r in rec.affected_resources if not valid_resources or r.lower() in valid_resources
        ]
        if len(kept_resources) != len(rec.affected_resources):
            violations.append(f"Removed unknown resources from recommendation '{rec.title}'.")
        rec.affected_resources = kept_resources
        rec.requires_human_review = True
        cleaned_recs.append(rec)

    if len(cleaned_recs) > max_recs:
        violations.append(f"Truncated recommendations from {len(cleaned_recs)} to {max_recs}.")
        cleaned_recs = cleaned_recs[:max_recs]

    # -- Numeric claims in prose ------------------------------------------
    # The model is not asked to count anything, but it sometimes does anyway.
    # Any sentence stating a number the evidence does not support is dropped
    # rather than published next to the correct deterministic total.
    allowed_numbers = _verifiable_numbers(episodes, events, coverage)

    kept_summary: list[str] = []
    for line in agent_report.executive_summary:
        unverified = _unverified_numbers(line, allowed_numbers)
        if unverified:
            violations.append(
                f"Dropped summary line stating unsupported number(s) {unverified}."
            )
            continue
        kept_summary.append(line)

    if not kept_summary:
        # Never render an empty summary; fall back to computed bullets.
        kept_summary = _deterministic_summary_lines(episodes, events, coverage)
        violations.append("Replaced the summary with deterministic counts.")

    unverified = _unverified_numbers(agent_report.headline, allowed_numbers)
    if unverified:
        violations.append(f"Replaced headline stating unsupported number(s) {unverified}.")
        agent_report.headline = _deterministic_headline(episodes, events)

    episode_summaries = {ep.episode_id: ep.deterministic_summary for ep in episodes}
    for analysis in cleaned_analyses:
        kept_facts: list[str] = []
        for fact in analysis.facts:
            unverified = _unverified_numbers(fact, allowed_numbers)
            if unverified:
                violations.append(
                    f"Dropped fact in {analysis.episode_id} stating "
                    f"unsupported number(s) {unverified}."
                )
                continue
            kept_facts.append(fact)
        if not kept_facts:
            fallback = episode_summaries.get(analysis.episode_id)
            kept_facts = [fallback] if fallback else []
        analysis.facts = kept_facts

    if critical:
        logger.warning("Validation failed critically: %s", violations)
        report = build_fallback_report(
            episodes, events, coverage, reason="AI report failed safety validation."
        )
        return ValidationOutcome(
            report=report,
            ai_status=AIStatus.FAILED_VALIDATION,
            fallback_used=True,
            violations=violations,
        )

    agent_report.episode_analyses = cleaned_analyses
    agent_report.recommendations = cleaned_recs
    agent_report.executive_summary = kept_summary
    logger.info("Validation passed with %d repair note(s)", len(violations))
    return ValidationOutcome(
        report=agent_report,
        ai_status=AIStatus.SUCCESS,
        fallback_used=False,
        violations=violations,
    )
