"""Anonymize run history for the public demo console.

Design constraint: the demo console must not publish account identifiers, IAM
principal names, or resource names, because those describe a live account's
inventory and where it is failing.

The approach is to keep every **number** real and replace every **free-text**
field with text derived only from those numbers. Trends, volumes, failure rates,
category mixes, and agent-health history are all genuine, so the charts show
real engineering. Nothing that could name a resource survives, because no
original string is passed through.

Two further protections:

* :func:`scrub` runs over anything that does reach output, replacing 12-digit
  account IDs, ARNs, and caller-configured name fragments. It is defense in
  depth, not the primary mechanism.
* The ``/report/<date>`` drill-through is disabled in demo mode by the handler.
  Stored HTML reports contain the full unredacted detail and are never exposed.
"""

from __future__ import annotations

import dataclasses
import os
import re
from typing import Iterable

from history import DayRecord

#: Shown at the top of the demo console so nobody mistakes it for live data.
DEMO_NOTICE = (
    "Public demo view. Volumes, failure counts, category mixes and agent-health "
    "history are real; account identifiers, principal names and resource names "
    "are withheld, and per-day report drill-through is disabled."
)

_ACCOUNT_RE = re.compile(r"\b\d{12}\b")
_ARN_RE = re.compile(r"arn:aws[a-z-]*:[^\s\"']+", re.IGNORECASE)
_KEY_RE = re.compile(r"account=[^/\s]*")

_CATEGORY_LABELS = {
    "DEPLOYMENT": "deployments",
    "FAILED_DEPLOYMENT": "failed deployments",
    "RESOURCE_LIFECYCLE": "resource lifecycle changes",
    "IAM_POLICY": "IAM and policy changes",
    "NETWORK": "network changes",
    "SECURITY": "security changes",
    "FAILED_OPERATION": "failed operations",
    "OTHER": "other activity",
}


def _extra_patterns() -> list[str]:
    """Caller-supplied name fragments to scrub, e.g. DEMO_SCRUB=awsug,myapp."""
    raw = os.environ.get("DEMO_SCRUB", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def sample_report_url() -> str:
    """A pre-sanitized report to link from the demo view.

    Per-day drill-through stays disabled in demo mode because stored reports
    hold real identifiers. This points at a separately published sample built
    from the demo scenario runner, so visitors can still see what a full report
    looks like. Only https URLs are accepted.
    """
    url = os.environ.get("DEMO_SAMPLE_REPORT_URL", "").strip()
    return url if url.startswith("https://") else ""


def scrub(text: str) -> str:
    """Remove identifiers from a string. Defense in depth."""
    if not text:
        return ""
    cleaned = _ARN_RE.sub("<arn>", text)
    cleaned = _KEY_RE.sub("account=<redacted>", cleaned)
    cleaned = _ACCOUNT_RE.sub("<account>", cleaned)
    for fragment in _extra_patterns():
        cleaned = re.sub(re.escape(fragment), "<name>", cleaned, flags=re.IGNORECASE)
    return cleaned


def _derived_headline(record: DayRecord) -> str:
    """Build a headline from counts only, so no original text is reused."""
    if record.status == "FAILED":
        return "Run failed; no analysis produced for this day."
    if record.episode_count == 0:
        return "No significant activity recorded."

    parts = [
        f"{record.episode_count} episode{'' if record.episode_count == 1 else 's'}"
        f" from {record.events_included} events"
    ]
    if record.failed_events:
        parts.append(f"{record.failed_events} failed")
    if record.episodes_by_category:
        top = max(record.episodes_by_category.items(), key=lambda kv: kv[1])
        label = _CATEGORY_LABELS.get(top[0], top[0].replace("_", " ").lower())
        parts.append(f"mostly {label}")
    if record.fallback_used:
        parts.append("deterministic fallback used")
    return "; ".join(parts) + "."


def anonymize_record(record: DayRecord, index: int) -> DayRecord:
    """Return a copy safe for public display.

    Numeric fields, statuses and category names pass through unchanged. Every
    free-text field is replaced rather than filtered.
    """
    return dataclasses.replace(
        record,
        # Sequential label; the real run ID encodes exact run timing.
        run_id=f"demo-run-{index:03d}",
        # Region names describe the account's footprint.
        failed_regions=[f"region-{i + 1}" for i in range(len(record.failed_regions))],
        # Derived from counts only.
        headline=_derived_headline(record),
        # The S3 key embeds the account ID.
        report_key="",
    )


def anonymize_records(records: Iterable[DayRecord]) -> list[DayRecord]:
    return [anonymize_record(r, i + 1) for i, r in enumerate(records)]


def is_demo_mode() -> bool:
    return os.environ.get("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
