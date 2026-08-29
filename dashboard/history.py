"""Read run history out of the report bucket.

The agent already writes everything this module needs. Each run stores a
structured ``report.json`` under::

    account=<id>/date=<YYYY-MM-DD>/run=<runId>/report.json

and each date gets a small ``completed.json`` marker pointing at the run that
won that date. Reading the markers first keeps the common path cheap: one
ListObjectsV2 per date partition plus one GetObject per day.

Nothing here writes to S3. The dashboard role is read-only by design.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

#: Cap on how many date partitions we will pull per request.
DEFAULT_MAX_DAYS = 60

#: Parallel GetObject workers. Reports are small; latency dominates.
_FETCH_WORKERS = 8


@dataclass
class DayRecord:
    """One day of history, flattened for display."""

    date: str
    run_id: str = ""
    status: str = "UNKNOWN"
    collection_status: str = "UNKNOWN"
    ai_status: str = "UNKNOWN"
    email_status: str = "UNKNOWN"
    fallback_used: bool = False
    partial: bool = False
    failed_regions: list[str] = field(default_factory=list)

    events_collected: int = 0
    events_included: int = 0
    failed_events: int = 0
    episode_count: int = 0
    episodes_by_category: dict[str, int] = field(default_factory=dict)

    headline: str = ""
    recommendation_count: int = 0
    recommendations_by_priority: dict[str, int] = field(default_factory=dict)
    tools_called: int = 0
    model_id: str = ""
    report_key: str = ""

    @property
    def dropped_events(self) -> int:
        """Events collected but filtered out before analysis."""
        return max(0, self.events_collected - self.events_included)

    @property
    def signal_ratio(self) -> float:
        """Share of collected events that survived filtering, as a percentage."""
        if self.events_collected <= 0:
            return 0.0
        return round(100.0 * self.events_included / self.events_collected, 1)

    @property
    def healthy(self) -> bool:
        return self.status in {"COMPLETE", "PARTIAL"} and not self.fallback_used


class HistoryReader:
    """Reads and aggregates stored reports."""

    def __init__(
        self,
        bucket: str,
        account_id: str,
        region: str,
        client: Any | None = None,
    ):
        self.bucket = bucket
        self.account_id = account_id
        self.client = client or boto3.client("s3", region_name=region)

    # -- low level ---------------------------------------------------------

    def _get_json(self, key: str) -> dict | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "AccessDenied"}:
                return None
            raise
        try:
            return json.loads(response["Body"].read())
        except (json.JSONDecodeError, KeyError):
            logger.warning("Unparseable JSON at %s", key)
            return None

    def list_dates(self, max_days: int = DEFAULT_MAX_DAYS) -> list[str]:
        """Newest-first list of report dates that have a date partition."""
        prefix = f"account={self.account_id}/"
        dates: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "Delimiter": "/",
            }
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            for entry in response.get("CommonPrefixes", []):
                # "account=123/date=2026-08-27/" -> "2026-08-27"
                segment = entry.get("Prefix", "").rstrip("/").rsplit("/", 1)[-1]
                if segment.startswith("date="):
                    dates.append(segment[len("date=") :])
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")

        dates.sort(reverse=True)
        return dates[:max_days]

    def _resolve_report_key(self, date: str) -> tuple[str, dict | None]:
        """Find the report.json for a date, plus its completed marker if any.

        Prefers the run named in ``completed.json``. Falls back to the lexically
        largest ``run=*/report.json``, which is the newest run because run IDs
        are timestamp-prefixed. The fallback matters for dates where the run
        failed before writing a completion marker.
        """
        marker = self._get_json(f"account={self.account_id}/date={date}/completed.json")
        if marker:
            uri = marker.get("reportS3Uri", "")
            prefix = f"s3://{self.bucket}/"
            if uri.startswith(prefix):
                return uri[len(prefix) :], marker

        keys = sorted(
            k
            for k in self._list_keys(f"account={self.account_id}/date={date}/")
            if k.endswith("report.json")
        )
        return (keys[-1] if keys else ""), marker

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
        return keys

    # -- aggregation -------------------------------------------------------

    def load_day(self, date: str) -> DayRecord | None:
        key, marker = self._resolve_report_key(date)
        if not key:
            return None
        report = self._get_json(key)
        if not report:
            return None
        return _to_record(date, key, report, marker)

    def load_history(self, max_days: int = DEFAULT_MAX_DAYS) -> list[DayRecord]:
        """Newest-first history. Days that fail to load are skipped."""
        dates = self.list_dates(max_days)
        if not dates:
            return []

        records: list[DayRecord] = []
        workers = min(_FETCH_WORKERS, len(dates))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for record in pool.map(self._safe_load_day, dates):
                if record is not None:
                    records.append(record)

        records.sort(key=lambda r: r.date, reverse=True)
        return records

    def _safe_load_day(self, date: str) -> DayRecord | None:
        try:
            return self.load_day(date)
        except Exception:  # noqa: BLE001 - one bad day must not break the page
            logger.warning("Failed to load history for %s", date, exc_info=True)
            return None

    def presigned_report_url(self, date: str, ttl_seconds: int = 900) -> str | None:
        """Mint a short-lived link to a day's rendered HTML report.

        Generated per request rather than stored: the signature is bound to this
        function's role session, so a long-lived URL would outlive its own
        credentials.
        """
        key, _ = self._resolve_report_key(date)
        if not key:
            return None
        html_key = key[: -len("report.json")] + "report.html"
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": html_key},
            ExpiresIn=ttl_seconds,
        )


def _to_record(
    date: str, key: str, report: dict, marker: dict | None
) -> DayRecord:
    """Flatten a stored report into a DayRecord.

    Tolerant by design: reports written before the dashboard existed have no
    ``totals.eventsCollected`` and no ``filterStats``, and the ``meta`` and
    ``collection`` blocks use snake_case while the rest of the document uses
    camelCase.
    """
    totals = report.get("totals") or {}
    collection = report.get("collection") or {}
    agent_execution = report.get("agentExecution") or {}
    body = report.get("report") or {}
    meta = report.get("meta") or {}
    marker = marker or {}

    recommendations = body.get("recommendations") or []
    by_priority: dict[str, int] = {}
    for rec in recommendations:
        priority = str(rec.get("priority", "LOW"))
        by_priority[priority] = by_priority.get(priority, 0) + 1

    events_included = int(totals.get("eventsIncluded") or 0)
    # Older reports predate eventsCollected; fall back to the included count so
    # the signal ratio degrades to 100% instead of dividing by zero.
    events_collected = int(totals.get("eventsCollected") or 0) or events_included

    return DayRecord(
        date=date,
        run_id=str(marker.get("runId") or meta.get("run_id") or ""),
        status=str(marker.get("status") or "UNKNOWN"),
        collection_status=str(
            marker.get("collectionStatus") or collection.get("status") or "UNKNOWN"
        ),
        ai_status=str(marker.get("aiStatus") or agent_execution.get("aiStatus") or "UNKNOWN"),
        email_status=str(marker.get("emailStatus") or "UNKNOWN"),
        fallback_used=bool(
            marker.get("fallbackUsed", agent_execution.get("fallbackUsed", False))
        ),
        partial=bool(collection.get("partial", False)),
        failed_regions=[
            str(f.get("region", "")) for f in (collection.get("failed_regions") or [])
        ],
        events_collected=events_collected,
        events_included=events_included,
        failed_events=int(totals.get("failedEvents") or 0),
        episode_count=int(totals.get("episodeCount") or 0),
        episodes_by_category=dict(totals.get("episodesByCategory") or {}),
        headline=str(body.get("headline") or ""),
        recommendation_count=len(recommendations),
        recommendations_by_priority=by_priority,
        tools_called=int(agent_execution.get("toolsCalled") or 0),
        model_id=str(agent_execution.get("modelId") or ""),
        report_key=key,
    )


def summarize(records: list[DayRecord]) -> dict:
    """Headline numbers across the whole window."""
    if not records:
        return {
            "days": 0,
            "eventsCollected": 0,
            "eventsIncluded": 0,
            "failedEvents": 0,
            "episodes": 0,
            "recommendations": 0,
            "aiSuccessRate": 0.0,
            "fallbackDays": 0,
            "partialDays": 0,
            "noiseFilteredPct": 0.0,
            "busiestDate": "",
            "categories": {},
        }

    collected = sum(r.events_collected for r in records)
    included = sum(r.events_included for r in records)
    ai_ok = sum(1 for r in records if r.ai_status == "SUCCESS")
    categories: dict[str, int] = {}
    for record in records:
        for name, count in record.episodes_by_category.items():
            categories[name] = categories.get(name, 0) + count

    busiest = max(records, key=lambda r: r.events_included)
    return {
        "days": len(records),
        "eventsCollected": collected,
        "eventsIncluded": included,
        "failedEvents": sum(r.failed_events for r in records),
        "episodes": sum(r.episode_count for r in records),
        "recommendations": sum(r.recommendation_count for r in records),
        "aiSuccessRate": round(100.0 * ai_ok / len(records), 1),
        "fallbackDays": sum(1 for r in records if r.fallback_used),
        "partialDays": sum(1 for r in records if r.partial),
        "noiseFilteredPct": (
            round(100.0 * (collected - included) / collected, 1) if collected else 0.0
        ),
        "busiestDate": busiest.date,
        "categories": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
    }


def resolve_bucket(account_id: str) -> str:
    """Mirror the agent's bucket resolution: env var, else account-scoped."""
    explicit = os.environ.get("REPORT_BUCKET", "").strip()
    return explicit or f"archival-agent-{account_id}"
