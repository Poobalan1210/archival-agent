"""Dispatcher Lambda for the Archival Agent (plan section 7).

Kept intentionally tiny. Triggered by EventBridge Scheduler, it validates the
payload, ensures us-east-1 is audited, generates run/session IDs, invokes the
AgentCore Runtime, and logs the compact result.

Depends only on boto3 (present in the Lambda runtime); it does NOT import the
agent package so it stays small and independently deployable.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import secrets

import boto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GLOBAL_SERVICE_REGION = "us-east-1"
JOB_TYPE = "DAILY_ARCHIVAL"

# A full run can take several minutes. Wait patiently for the synchronous
# InvokeAgentRuntime response and, critically, DO NOT retry: a retry on a slow
# (but succeeding) run would start a second, duplicate run.
_INVOKE_CONFIG = BotoConfig(
    connect_timeout=10,
    read_timeout=870,
    retries={"max_attempts": 0, "mode": "standard"},
)

_client = None


def _agentcore_client():
    global _client
    if _client is None:
        region = os.environ.get("AGENT_REGION", GLOBAL_SERVICE_REGION)
        _client = boto3.client("bedrock-agentcore", region_name=region, config=_INVOKE_CONFIG)
    return _client


def _generate_run_id(now: _dt.datetime) -> str:
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _generate_session_id(run_id: str) -> str:
    # AgentCore requires runtimeSessionId to be 33-256 characters.
    session_id = f"archival-agent-session-{run_id}"
    if len(session_id) < 33:
        session_id = (session_id + "-" + secrets.token_hex(8))[:256]
    return session_id


def _effective_regions(audit_regions: list[str], include_global: bool = True) -> list[str]:
    regions = list(audit_regions or [])
    if include_global and GLOBAL_SERVICE_REGION not in regions:
        regions.append(GLOBAL_SERVICE_REGION)
    seen: set[str] = set()
    ordered: list[str] = []
    for region in regions:
        if region and region not in seen:
            seen.add(region)
            ordered.append(region)
    return ordered


def _read_response(response: dict) -> dict:
    """Read the Runtime response body into a dict."""
    body = response.get("response")
    if body is None:
        return {}
    if hasattr(body, "read"):
        raw = body.read()
    elif isinstance(body, (bytes, bytearray, str)):
        raw = body
    else:  # event stream fallback
        raw = b"".join(chunk for chunk in body)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"raw": raw}


def handler(event: dict, context) -> dict:
    """Lambda entry point invoked by EventBridge Scheduler."""
    event = event or {}
    runtime_arn = os.environ.get("AGENT_RUNTIME_ARN")
    if not runtime_arn:
        raise RuntimeError("AGENT_RUNTIME_ARN environment variable is not set.")

    now = _dt.datetime.now(_dt.timezone.utc)
    run_id = _generate_run_id(now)
    session_id = _generate_session_id(run_id)

    timezone = event.get("timezone", "UTC")
    include_global = bool(event.get("includeGlobalRegion", True))
    audit_regions = _effective_regions(event.get("auditRegions", []), include_global)
    force = bool(event.get("force", False))
    # Which local day to report on: 1 = previous day (default), 0 = today.
    window_days_ago = int(event.get("windowDaysAgo", 1))

    payload = {
        "jobType": JOB_TYPE,
        "runId": run_id,
        "sessionId": session_id,
        "timezone": timezone,
        "auditRegions": audit_regions,
        "force": force,
        "windowDaysAgo": window_days_ago,
        "includeGlobalRegion": include_global,
    }

    logger.info("dispatching run=%s regions=%s tz=%s force=%s", run_id, audit_regions, timezone, force)

    kwargs = {
        "agentRuntimeArn": runtime_arn,
        "runtimeSessionId": session_id,
        "payload": json.dumps(payload).encode("utf-8"),
        "contentType": "application/json",
        "accept": "application/json",
    }
    qualifier = os.environ.get("AGENT_RUNTIME_QUALIFIER")
    if qualifier:
        kwargs["qualifier"] = qualifier

    try:
        response = _agentcore_client().invoke_agent_runtime(**kwargs)
    except Exception:
        logger.exception("run=%s | InvokeAgentRuntime failed", run_id)
        raise

    result = _read_response(response)
    logger.info(
        "run=%s | runtime status=%s http=%s result=%s",
        run_id,
        result.get("status"),
        response.get("statusCode"),
        json.dumps(result, default=str),
    )
    return {
        "runId": run_id,
        "sessionId": session_id,
        "runtimeStatusCode": response.get("statusCode"),
        "result": result,
    }
