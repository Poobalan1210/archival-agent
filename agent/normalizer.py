"""Normalization and redaction of raw CloudTrail events (plan section 11).

Each raw ``lookup_events`` record carries a ``CloudTrailEvent`` JSON string with
the authoritative event. We parse it, redact sensitive values recursively,
allowlist request parameters, and emit a compact :class:`NormalizedEvent`.

Security note: every value here is untrusted input. It is treated as data only
and must never be interpreted as instructions by downstream components.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from datetime import datetime, timezone
from typing import Any

from agent.config import Config
from agent.models.event import NormalizedEvent, Principal, ResourceRef, SourceInfo
from agent.models.enums import EventResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact(value: Any, rules: dict, _path: str = "") -> Any:
    """Recursively redact sensitive keys from a nested structure.

    A key is redacted when its lowercased name contains any denylisted
    substring, or when its dotted path matches a denylisted path.
    """
    substrings: list[str] = [s.lower() for s in rules.get("substrings", [])]
    paths: list[str] = [p.lower() for p in rules.get("paths", [])]
    placeholder: str = rules.get("placeholder", "[REDACTED]")

    def _walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            redacted: dict[str, Any] = {}
            for key, sub in node.items():
                key_lower = str(key).lower()
                current = f"{path}.{key_lower}" if path else key_lower
                if _matches(key_lower, current, substrings, paths):
                    redacted[key] = placeholder
                else:
                    redacted[key] = _walk(sub, current)
            return redacted
        if isinstance(node, list):
            return [_walk(item, path) for item in node]
        return node

    return _walk(value, _path)


def _matches(key_lower: str, path: str, substrings: list[str], paths: list[str]) -> bool:
    if any(sub in key_lower for sub in substrings):
        return True
    for denied in paths:
        if path == denied or path.endswith(f".{denied}"):
            return True
    return False


# ---------------------------------------------------------------------------
# Request-parameter allowlisting
# ---------------------------------------------------------------------------


def apply_request_allowlist(
    request_params: dict | None, event_source: str, allowlist: dict
) -> dict:
    """Keep only allowlisted top-level request keys (plan section 11)."""
    if not request_params or not isinstance(request_params, dict):
        return {}
    allowed_global = set(allowlist.get("global", []))
    allowed_source = set(allowlist.get("by_source", {}).get(event_source, []))
    allowed = allowed_global | allowed_source
    return {key: val for key, val in request_params.items() if key in allowed}


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def _extract_principal(user_identity: dict | None) -> Principal:
    if not user_identity:
        return Principal()

    ptype = user_identity.get("type")
    name = user_identity.get("userName")
    arn = user_identity.get("arn")

    if not name and arn and "assumed-role/" in arn:
        # arn:aws:sts::acct:assumed-role/RoleName/SessionName -> RoleName/SessionName
        name = arn.split("assumed-role/", 1)[-1]
    if not name:
        session_issuer = (user_identity.get("sessionContext") or {}).get("sessionIssuer") or {}
        name = session_issuer.get("userName")
    if not name and arn:
        name = arn.rsplit("/", 1)[-1] if "/" in arn else arn
    if ptype == "AWSService":
        name = user_identity.get("invokedBy") or name
    if ptype == "Root" and not name:
        name = "root"

    return Principal(
        type=ptype,
        name=name,
        invokedBy=user_identity.get("invokedBy"),
        accountId=user_identity.get("accountId"),
        principalId=user_identity.get("principalId"),
    )


def _session_key(user_identity: dict | None) -> str | None:
    if not user_identity:
        return None
    if user_identity.get("accessKeyId"):
        return user_identity["accessKeyId"]
    session_context = user_identity.get("sessionContext") or {}
    attributes = session_context.get("attributes") or {}
    if attributes.get("creationDate"):
        return f"{user_identity.get('principalId', 'unknown')}:{attributes['creationDate']}"
    return user_identity.get("principalId")


def _classify_source_ip(source_ip: str | None) -> str | None:
    if not source_ip:
        return None
    if source_ip.endswith(".amazonaws.com") or source_ip.lower() == "aws internal":
        return "AWS_INTERNAL"
    try:
        address = ipaddress.ip_address(source_ip)
    except ValueError:
        return "UNKNOWN"
    return "PRIVATE" if address.is_private else "PUBLIC"


# Request-parameter keys that commonly carry a resource identifier, used to
# enrich events that lack a `resources` array (e.g. Lambda config changes).
_RESOURCE_NAME_KEYS = (
    "functionName",
    "roleName",
    "userName",
    "groupName",
    "policyName",
    "bucketName",
    "queueName",
    "topicName",
    "tableName",
    "clusterName",
    "keyId",
    "stackName",
    "groupId",
    "instanceId",
    "name",
)


def _extract_resources(parsed: dict, raw: dict, request: dict, rules: dict) -> list[ResourceRef]:
    resources: list[ResourceRef] = []
    seen: set[str] = set()

    for entry in parsed.get("resources") or []:
        arn = entry.get("ARN") or entry.get("arn")
        rtype = entry.get("type") or entry.get("resourceType")
        name = entry.get("resourceName") or entry.get("accountId")
        if not name and arn:
            name = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        marker = arn or name or ""
        if marker and marker not in seen:
            seen.add(marker)
            resources.append(ResourceRef(type=rtype, name=name, arn=arn))

    # Fall back to the lookup_events Resources array.
    for entry in raw.get("Resources") or []:
        name = entry.get("ResourceName")
        rtype = entry.get("ResourceType")
        marker = name or ""
        if marker and marker not in seen:
            seen.add(marker)
            resources.append(ResourceRef(type=rtype, name=name, arn=None))

    # Enrich from request parameters when no resource array was present.
    if not resources:
        for key in _RESOURCE_NAME_KEYS:
            value = request.get(key)
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                resources.append(ResourceRef(type=None, name=value, arn=None))
                break

    return resources


def _parse_event_time(parsed: dict, raw: dict) -> datetime:
    value = parsed.get("eventTime") or raw.get("EventTime")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            parsed_dt = datetime.fromisoformat(text)
            return parsed_dt if parsed_dt.tzinfo else parsed_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_event(raw: dict, config: Config) -> NormalizedEvent | None:
    """Normalize a single raw ``lookup_events`` record.

    Returns ``None`` if the record cannot be parsed.
    """
    redaction_rules = config.redaction_fields
    allowlist = config.request_allowlist

    cloudtrail_event = raw.get("CloudTrailEvent")
    parsed: dict = {}
    if isinstance(cloudtrail_event, str):
        try:
            parsed = json.loads(cloudtrail_event)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to parse CloudTrailEvent for %s", raw.get("EventId"))
            parsed = {}
    elif isinstance(cloudtrail_event, dict):
        parsed = cloudtrail_event

    event_source = parsed.get("eventSource") or raw.get("EventSource") or "unknown"
    event_name = parsed.get("eventName") or raw.get("EventName") or "Unknown"
    error_code = parsed.get("errorCode")

    # Redact then allowlist request parameters.
    request_params = parsed.get("requestParameters")
    redacted_request = redact(request_params, redaction_rules) if request_params else {}
    request = apply_request_allowlist(redacted_request, event_source, allowlist)

    user_identity = parsed.get("userIdentity") or {}
    principal = _extract_principal(user_identity)

    source = SourceInfo(
        userAgent=parsed.get("userAgent") or raw.get("UserAgent"),
        sourceIp=parsed.get("sourceIPAddress"),
        sourceIpType=_classify_source_ip(parsed.get("sourceIPAddress")),
    )

    read_only = parsed.get("readOnly")
    if read_only is None:
        read_only = str(raw.get("ReadOnly", "")).lower() == "true"

    try:
        return NormalizedEvent(
            eventId=parsed.get("eventID") or raw.get("EventId") or "unknown",
            eventTime=_parse_event_time(parsed, raw),
            region=parsed.get("awsRegion") or raw.get("AwsRegion") or "unknown",
            eventSource=event_source,
            eventName=event_name,
            eventCategory=parsed.get("eventCategory") or "Management",
            readOnly=bool(read_only),
            result=EventResult.FAILURE if error_code else EventResult.SUCCESS,
            principal=principal,
            source=source,
            resources=_extract_resources(parsed, raw, request, redaction_rules),
            request=request,
            error=error_code and f"{error_code}: {parsed.get('errorMessage', '')}".strip(),
            errorCode=error_code,
            requestId=parsed.get("requestID") or raw.get("RequestId"),
            sessionKey=_session_key(user_identity),
        )
    except Exception:  # noqa: BLE001 - never let one bad record abort the run
        logger.exception("Failed to normalize event %s", raw.get("EventId"))
        return None


def normalize_and_redact(raw_events: list[dict], config: Config) -> list[NormalizedEvent]:
    """Normalize a batch of raw events, dropping any that fail to parse."""
    normalized: list[NormalizedEvent] = []
    for raw in raw_events:
        event = normalize_event(raw, config)
        if event is not None:
            normalized.append(event)
    logger.info("Normalized %d of %d raw events", len(normalized), len(raw_events))
    return normalized
