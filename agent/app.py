"""AgentCore Runtime entry point and run orchestration (plan section 8).

``run_archival`` is the framework-agnostic orchestration; it is pure enough to
test with injected AWS clients and a fake agent. ``invoke`` is the thin
``@app.entrypoint`` wrapper the AgentCore Runtime calls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import boto3
from bedrock_agentcore import BedrockAgentCoreApp

from agent.analyst import analyze_with_strands
from agent.collector import collect_events
from agent.config import Config, load_config
from agent.filters import filter_events
from agent.grouper import group_episodes
from agent.idempotency import (
    acquire_lock,
    already_processed,
    load_previous_report,
    mark_completed,
    release_lock,
)
from agent.models.enums import AIStatus, CollectionStatus, EmailStatus, RunStatus
from agent.models.request import DailyJobRequest, DailyJobResult, RunContext
from agent.normalizer import normalize_and_redact
from agent.notifier import build_subject, send_email
from agent.renderer import (
    build_render_context,
    build_report_json,
    render_email_html,
    render_report_html,
)
from agent.storage import ReportStorage
from agent.validator import validate_or_fallback
from agent.tools.context import serialize_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("archival_agent")

app = BedrockAgentCoreApp()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_account_id(config: Config, sts_client: Any | None = None) -> str:
    if config.account_id:
        return config.account_id
    client = sts_client or boto3.client("sts", region_name=config.agent_region)
    return client.get_caller_identity()["Account"]


def _bucket_name(config: Config, account_id: str) -> str:
    return config.report_bucket or f"archival-agent-{account_id}"


def _agent_execution(config: Config, ai_status, tools_called, fallback_used, notes) -> dict:
    return {
        "framework": "Strands",
        "runtime": "Amazon Bedrock AgentCore Runtime",
        "model": "Amazon Nova Pro",
        "modelId": config.nova_model_id,
        "aiStatus": str(ai_status),
        "toolsCalled": tools_called,
        "fallbackUsed": fallback_used,
        "validationNotes": notes,
    }


def _store_artifacts(
    storage: ReportStorage,
    prefix: str,
    included_events,
    episodes,
    report_json: dict,
    report_html: str,
) -> str:
    """Persist the run artifacts; returns the report.json S3 URI."""
    storage.put_json(
        prefix + "events.json",
        [serialize_event(e) for e in included_events],
    )
    storage.put_json(
        prefix + "episodes.json",
        [ep.model_dump(by_alias=True, mode="json") for ep in episodes],
    )
    report_uri = storage.put_json(prefix + "report.json", report_json)
    storage.put_html(prefix + "report.html", report_html)
    return report_uri


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_archival(
    request: DailyJobRequest,
    config: Config,
    *,
    cloudtrail_client_factory: Callable[[str], Any] | None = None,
    s3_client: Any | None = None,
    ses_client: Any | None = None,
    sts_client: Any | None = None,
    agent: Any | None = None,
    now: datetime | None = None,
) -> DailyJobResult:
    """Execute one daily Archival Agent run end to end."""
    account_id = _resolve_account_id(config, sts_client)
    context = RunContext.from_request(request, account_id, now=now)
    storage = ReportStorage(_bucket_name(config, account_id), config.agent_region, client=s3_client)

    logger.info(
        "run=%s date=%s regions=%s force=%s | starting run",
        context.run_id,
        context.report_date,
        context.audit_regions,
        request.force,
    )

    # -- Idempotency ------------------------------------------------------
    if not request.force and already_processed(storage, config, context.report_date):
        logger.info("date=%s | already processed; skipping", context.report_date)
        return DailyJobResult(
            runId=context.run_id,
            status=RunStatus.ALREADY_PROCESSED,
            reportDate=context.report_date,
            sessionId=context.session_id,
            message="A completed report already exists for this date.",
        )

    if not acquire_lock(storage, config, context.report_date, context.run_id):
        if not request.force:
            return DailyJobResult(
                runId=context.run_id,
                status=RunStatus.ALREADY_PROCESSED,
                reportDate=context.report_date,
                sessionId=context.session_id,
                message="Another run holds the lock for this date.",
            )

    try:
        # -- Collect / normalize / filter / group -------------------------
        raw_events, coverage = collect_events(
            context, config, client_factory=cloudtrail_client_factory
        )

        if coverage.status == CollectionStatus.FAILED:
            return _handle_collection_failure(
                context, config, storage, coverage, ses_client
            )

        normalized = normalize_and_redact(raw_events, config)
        included, filter_stats = filter_events(normalized, config)
        episodes = group_episodes(included, config)

        # -- Analyze (Strands + Nova Pro) ---------------------------------
        def previous_loader(days_ago: int):
            return load_previous_report(storage, config, context.report_date, days_ago)

        analysis = analyze_with_strands(
            context,
            coverage,
            episodes,
            included,
            config,
            filter_stats=filter_stats,
            previous_report_loader=previous_loader,
            agent=agent,
        )

        # -- Validate or fall back ----------------------------------------
        outcome = validate_or_fallback(
            analysis.report,
            episodes,
            included,
            coverage,
            config,
            ai_failed=(analysis.ai_status == AIStatus.FAILED),
        )
        agent_execution = _agent_execution(
            config,
            outcome.ai_status,
            analysis.tools_called,
            outcome.fallback_used,
            outcome.violations,
        )

        # -- Render / store -----------------------------------------------
        render_ctx = build_render_context(
            context,
            coverage,
            outcome.report,
            episodes,
            included,
            agent_execution,
            config,
            events_collected=len(raw_events),
            filter_stats=filter_stats,
        )
        report_json = build_report_json(render_ctx)
        report_html = render_report_html(render_ctx)

        prefix = config.s3_run_prefix(context.report_date, context.run_id)
        report_uri = _store_artifacts(
            storage, prefix, included, episodes, report_json, report_html
        )

        # Presigned link for the email, then render the email.
        presigned = storage.presigned_url(
            prefix + "report.html", config.presigned_url_ttl_seconds
        )
        render_ctx["presigned_url"] = presigned
        email_html = render_email_html(render_ctx)

        # -- Notify -------------------------------------------------------
        failures = report_json["totals"]["failedEvents"]
        subject = build_subject(outcome.report, coverage, context.report_date, failures)
        email_status = send_email(config, subject, email_html, outcome.report, client=ses_client)

        overall_status = (
            RunStatus.PARTIAL
            if coverage.status == CollectionStatus.PARTIAL
            else RunStatus.COMPLETE
        )

        # -- status.json + completion marker ------------------------------
        status_doc = {
            "status": str(overall_status),
            "collectionStatus": str(coverage.status),
            "aiStatus": str(outcome.ai_status),
            "fallbackUsed": outcome.fallback_used,
            "emailStatus": str(email_status),
        }
        storage.put_json(prefix + "status.json", status_doc)
        mark_completed(
            storage,
            config,
            context.report_date,
            {**status_doc, "runId": context.run_id, "reportS3Uri": report_uri},
        )

        logger.info("run=%s | complete status=%s", context.run_id, overall_status)
        return DailyJobResult(
            runId=context.run_id,
            status=overall_status,
            reportDate=context.report_date,
            collectionStatus=coverage.status,
            aiStatus=outcome.ai_status,
            eventsCollected=len(raw_events),
            eventsIncluded=len(included),
            episodesGenerated=len(episodes),
            reportS3Uri=report_uri,
            emailStatus=email_status,
            fallbackUsed=outcome.fallback_used,
            toolsCalled=analysis.tools_called,
            sessionId=context.session_id,
        )
    except Exception as exc:  # noqa: BLE001 - report failure, never claim success
        logger.exception("run=%s | failed", context.run_id)
        return DailyJobResult(
            runId=context.run_id,
            status=RunStatus.FAILED,
            reportDate=context.report_date,
            sessionId=context.session_id,
            message=f"{type(exc).__name__}: {exc}",
        )
    finally:
        release_lock(storage, config, context.report_date)


def _handle_collection_failure(
    context: RunContext,
    config: Config,
    storage: ReportStorage,
    coverage,
    ses_client,
) -> DailyJobResult:
    """All regions failed: skip AI, notify, and do not mark completed."""
    logger.error("run=%s | all regions failed collection", context.run_id)
    outcome = validate_or_fallback(None, [], [], coverage, config, ai_failed=True)
    agent_execution = _agent_execution(config, AIStatus.SKIPPED, 0, True, ["Collection failed."])
    render_ctx = build_render_context(
        context, coverage, outcome.report, [], [], agent_execution, config
    )
    subject = build_subject(outcome.report, coverage, context.report_date, 0)
    email_status = send_email(
        config, f"[FAILED] {subject}", render_email_html(render_ctx), outcome.report, client=ses_client
    )
    return DailyJobResult(
        runId=context.run_id,
        status=RunStatus.FAILED,
        reportDate=context.report_date,
        collectionStatus=CollectionStatus.FAILED,
        aiStatus=AIStatus.SKIPPED,
        emailStatus=email_status,
        sessionId=context.session_id,
        message="All audit regions failed collection; no report generated.",
    )


# ---------------------------------------------------------------------------
# AgentCore entry point
# ---------------------------------------------------------------------------


@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore Runtime entry point.

    An empty payload or a health/ping job returns a health status so the runtime
    can be smoke-tested. A DAILY_ARCHIVAL payload runs the full job.
    """
    payload = payload or {}
    job_type = payload.get("jobType")
    if job_type in (None, "HEALTH_CHECK", "PING"):
        return {"status": "HEALTHY", "service": "archival-agent"}

    config = load_config()
    request = DailyJobRequest.model_validate(payload)
    result = run_archival(request, config)
    return result.to_payload()


if __name__ == "__main__":
    app.run()
