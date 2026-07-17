"""Idempotency and duplicate-run protection (plan section 21).

Before processing, we check for a ``completed.json`` marker for the report date.
A conditional ``lock.json`` object prevents two simultaneous runs. A forced
invocation (``force=true``) bypasses the completed check and uses a new run ID.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from agent.config import Config
from agent.storage import ReportStorage

logger = logging.getLogger(__name__)


def already_processed(storage: ReportStorage, config: Config, report_date: str) -> bool:
    """True if a completed marker exists for the report date."""
    return storage.exists(config.s3_completed_marker(report_date))


def acquire_lock(
    storage: ReportStorage, config: Config, report_date: str, run_id: str
) -> bool:
    """Try to claim the run lock. Returns False if another run holds it."""
    key = config.s3_lock_key(report_date)
    acquired = storage.put_if_absent(
        key,
        {"runId": run_id, "acquiredAt": datetime.utcnow().isoformat() + "Z"},
    )
    if not acquired:
        logger.warning("Lock already held for %s; another run is in progress", report_date)
    return acquired


def release_lock(storage: ReportStorage, config: Config, report_date: str) -> None:
    storage.delete(config.s3_lock_key(report_date))


def mark_completed(
    storage: ReportStorage, config: Config, report_date: str, summary: dict
) -> None:
    """Write the completed marker so future non-forced runs skip this date."""
    storage.put_json(config.s3_completed_marker(report_date), summary)


def load_previous_report(
    storage: ReportStorage, config: Config, report_date: str, days_ago: int
) -> dict | None:
    """Load a previous day's report.json from S3, if present.

    Looks under the account/date prefix for ``days_ago`` before ``report_date``
    and returns the most recent run's structured report.
    """
    try:
        base = datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        return None
    target = (base - timedelta(days=max(1, days_ago))).strftime("%Y-%m-%d")

    prefix = f"account={config.account_id}/date={target}/"
    report_keys = sorted(k for k in storage.list_keys(prefix) if k.endswith("report.json"))
    if not report_keys:
        return None
    # Latest run wins (run IDs are timestamp-prefixed, so lexical max is newest).
    return storage.get_json(report_keys[-1])
