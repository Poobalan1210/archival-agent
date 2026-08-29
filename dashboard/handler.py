"""Archival Agent console: a read-only history view over stored reports.

Routes (Lambda Function URL, payload format 2.0):

    GET /                  Rendered history dashboard
    GET /api/history.json  The same aggregates as JSON
    GET /report/<date>     302 to a short-lived presigned link to that day's HTML
    GET /healthz           Liveness probe, no auth

Access control
--------------
Reports are derived from CloudTrail and name IAM principals, resource IDs, and
security-group changes. The Function URL therefore requires a shared token,
supplied either as ``Authorization: Bearer <token>`` or ``?token=<token>``, and
compared with :func:`hmac.compare_digest`.

If ``DASHBOARD_TOKEN`` is unset the handler fails closed with 503 rather than
serving audit data anonymously. ``/healthz`` is the only unauthenticated route
and reveals nothing beyond liveness.

Demo mode
---------
With ``DEMO_MODE=true`` the handler serves a public, anonymized view: no token is
required, every record passes through :mod:`anonymize` so identifiers and
free-text fields are replaced with values derived from counts, and the
``/report/<date>`` drill-through is disabled because stored HTML reports contain
full unredacted detail. Demo mode is set per-function in the SAM template, so the
public endpoint cannot be made to serve real strings by crafting a request.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from typing import Any

from anonymize import DEMO_NOTICE, anonymize_records, is_demo_mode
from history import (
    DEFAULT_MAX_DAYS,
    HistoryReader,
    resolve_bucket,
    summarize,
)
from render import render_dashboard

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Presigned links are for immediate clicking, so keep the window short.
_REPORT_LINK_TTL_SECONDS = 900

_reader: HistoryReader | None = None


def _account_id() -> str:
    account = os.environ.get("AWS_ACCOUNT_ID", "").strip()
    if account:
        return account
    import boto3

    region = os.environ.get("AGENT_REGION", "us-east-1")
    return boto3.client("sts", region_name=region).get_caller_identity()["Account"]


def _get_reader() -> HistoryReader:
    """Build the reader once per container and reuse it across invocations."""
    global _reader
    if _reader is None:
        account = _account_id()
        region = os.environ.get("AGENT_REGION", "us-east-1")
        _reader = HistoryReader(resolve_bucket(account), account, region)
    return _reader


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------


def _response(
    status: int,
    body: str,
    content_type: str = "text/html; charset=utf-8",
    extra_headers: dict[str, str] | None = None,
) -> dict:
    headers = {
        "content-type": content_type,
        # The page is self-contained: inline SVG and inline CSS, no scripts.
        "content-security-policy": (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        ),
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "cache-control": "no-store",
    }
    if extra_headers:
        headers.update(extra_headers)
    return {"statusCode": status, "headers": headers, "body": body}


def _json_response(status: int, payload: dict) -> dict:
    return _response(
        status, json.dumps(payload, indent=2, default=str), "application/json"
    )


def _path(event: dict) -> str:
    raw = (
        event.get("rawPath")
        or event.get("path")
        or (event.get("requestContext", {}).get("http", {}) or {}).get("path")
        or "/"
    )
    # Function URLs do not use stages, but strip a trailing slash for matching.
    return raw.rstrip("/") or "/"


def _method(event: dict) -> str:
    http = event.get("requestContext", {}).get("http", {}) or {}
    return (http.get("method") or event.get("httpMethod") or "GET").upper()


def _presented_token(event: dict) -> str:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth:
        return auth.strip()
    params = event.get("queryStringParameters") or {}
    return (params.get("token") or "").strip()


def _authorized(event: dict) -> tuple[bool, str]:
    """(allowed, reason). Fails closed when no token is configured.

    Demo mode is public by design: it only ever serves anonymized aggregates.
    """
    if is_demo_mode():
        return True, "demo"
    expected = os.environ.get("DASHBOARD_TOKEN", "").strip()
    if not expected:
        return False, "unconfigured"
    presented = _presented_token(event)
    if not presented:
        return False, "missing"
    if not hmac.compare_digest(presented, expected):
        return False, "mismatch"
    return True, "ok"


def _max_days(event: dict) -> int:
    params = event.get("queryStringParameters") or {}
    raw = (params.get("days") or "").strip()
    if not raw:
        return DEFAULT_MAX_DAYS
    try:
        return max(1, min(365, int(raw)))
    except ValueError:
        return DEFAULT_MAX_DAYS


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    event = event or {}
    path = _path(event)
    method = _method(event)

    if path == "/healthz":
        return _json_response(200, {"status": "ok", "service": "archival-agent-dashboard"})

    if method not in {"GET", "HEAD"}:
        return _response(405, "<h1>405 Method Not Allowed</h1>")

    allowed, reason = _authorized(event)
    if not allowed:
        if reason == "unconfigured":
            logger.error("DASHBOARD_TOKEN is not set; refusing to serve report data.")
            return _response(
                503,
                "<h1>503 Not Configured</h1><p>This console has no access token "
                "configured, so it will not serve report data.</p>",
            )
        logger.warning("Rejected unauthorized request to %s (%s)", path, reason)
        return _response(
            401,
            "<h1>401 Unauthorized</h1><p>A valid access token is required.</p>",
            extra_headers={"www-authenticate": 'Bearer realm="archival-agent"'},
        )

    try:
        return _route(event, path)
    except Exception:  # noqa: BLE001 - never leak a stack trace to the browser
        logger.exception("Unhandled error serving %s", path)
        return _response(500, "<h1>500 Internal Server Error</h1>")


def _route(event: dict, path: str) -> dict:
    reader = _get_reader()
    demo = is_demo_mode()

    if path.startswith("/report/"):
        # Stored HTML reports carry full unredacted detail, so the demo console
        # never links to them.
        if demo:
            return _response(
                404,
                "<h1>404 Not Found</h1><p>Per-day reports are not available in "
                "the public demo view.</p>",
            )
        date = path[len("/report/") :]
        if not _DATE_RE.match(date):
            return _response(400, "<h1>400 Bad Request</h1><p>Expected /report/YYYY-MM-DD.</p>")
        url = reader.presigned_report_url(date, _REPORT_LINK_TTL_SECONDS)
        if not url:
            return _response(404, f"<h1>404 Not Found</h1><p>No report stored for {date}.</p>")
        return {
            "statusCode": 302,
            "headers": {"location": url, "cache-control": "no-store"},
            "body": "",
        }

    records = reader.load_history(_max_days(event))
    if demo:
        records = anonymize_records(records)

    if path == "/api/history.json":
        return _json_response(
            200,
            {
                "demo": demo,
                **({"notice": DEMO_NOTICE} if demo else {}),
                "summary": summarize(records),
                "days": [
                    {
                        "date": r.date,
                        "runId": r.run_id,
                        "status": r.status,
                        "collectionStatus": r.collection_status,
                        "aiStatus": r.ai_status,
                        "fallbackUsed": r.fallback_used,
                        "partial": r.partial,
                        "failedRegions": r.failed_regions,
                        "eventsCollected": r.events_collected,
                        "eventsIncluded": r.events_included,
                        "droppedEvents": r.dropped_events,
                        "signalRatio": r.signal_ratio,
                        "failedEvents": r.failed_events,
                        "episodeCount": r.episode_count,
                        "episodesByCategory": r.episodes_by_category,
                        "recommendationCount": r.recommendation_count,
                        "recommendationsByPriority": r.recommendations_by_priority,
                        "toolsCalled": r.tools_called,
                        "modelId": r.model_id,
                        "headline": r.headline,
                    }
                    for r in records
                ],
            },
        )

    if path == "/":
        # No token is threaded through in demo mode; there are no links to sign.
        token = "" if demo else _presented_token(event)
        return _response(
            200,
            render_dashboard(records, summarize(records), token, demo=demo),
        )

    return _response(404, "<h1>404 Not Found</h1>")
