"""Invocation, result, and run-context models (plan sections 6-10).

``DailyJobRequest`` is the payload the AgentCore Runtime receives from the
dispatcher. ``RunContext`` is the derived, validated state used throughout a
single run. ``DailyJobResult`` is the compact object returned to the dispatcher.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.config import GLOBAL_SERVICE_REGION
from agent.models.enums import (
    AIStatus,
    CollectionStatus,
    EmailStatus,
    JobType,
    RunStatus,
)


def generate_run_id(now: datetime | None = None) -> str:
    """Create a sortable run ID like ``20260718-013000-a4c2``."""
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def generate_session_id(run_id: str) -> str:
    """Derive an AgentCore session ID from a run ID.

    AgentCore session IDs are bounded; keep this compact and prefixed.
    """
    return f"archival-agent-session-{run_id}"[:100]


class DailyJobRequest(BaseModel):
    """Payload delivered to the Runtime (superset of the scheduler payload)."""

    model_config = ConfigDict(populate_by_name=True)

    job_type: JobType = Field(default=JobType.DAILY_ARCHIVAL, alias="jobType")
    run_id: str | None = Field(default=None, alias="runId")
    session_id: str | None = Field(default=None, alias="sessionId")
    timezone: str = "UTC"
    audit_regions: list[str] = Field(default_factory=list, alias="auditRegions")
    force: bool = False
    # Which local day to report on: 1 = previous day (the scheduled default),
    # 0 = the current day so far (useful for demos/backfills), N = N days ago.
    window_days_ago: int = Field(default=1, ge=0, alias="windowDaysAgo")
    # Whether to always add us-east-1 (where global-service events like IAM are
    # recorded). Default True for production. Set False to scope a run to only
    # the listed audit regions (e.g. an isolated demo in one region).
    include_global_region: bool = Field(default=True, alias="includeGlobalRegion")

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        # Raises ZoneInfoNotFoundError early if the timezone is invalid.
        ZoneInfo(value)
        return value

    def effective_regions(self) -> list[str]:
        """Audit regions with the mandatory global-service region added.

        Order preserved, duplicates removed. See plan section 5.
        """
        regions = list(self.audit_regions)
        if self.include_global_region and GLOBAL_SERVICE_REGION not in regions:
            regions.append(GLOBAL_SERVICE_REGION)
        seen: set[str] = set()
        ordered: list[str] = []
        for region in regions:
            if region and region not in seen:
                seen.add(region)
                ordered.append(region)
        return ordered


class RegionFailure(BaseModel):
    region: str
    error: str


class CollectionCoverage(BaseModel):
    """Which regions were queried and how it went (plan section 10)."""

    model_config = ConfigDict(populate_by_name=True)

    requested_regions: list[str] = Field(default_factory=list, alias="requestedRegions")
    completed_regions: list[str] = Field(default_factory=list, alias="completedRegions")
    failed_regions: list[RegionFailure] = Field(default_factory=list, alias="failedRegions")
    status: CollectionStatus = CollectionStatus.COMPLETE

    def finalize(self) -> "CollectionCoverage":
        """Derive the overall status from completed/failed regions."""
        if not self.completed_regions and self.failed_regions:
            self.status = CollectionStatus.FAILED
        elif self.failed_regions:
            self.status = CollectionStatus.PARTIAL
        else:
            self.status = CollectionStatus.COMPLETE
        return self


class RunContext(BaseModel):
    """Derived, validated state for a single run."""

    model_config = ConfigDict(populate_by_name=True)

    account_id: str
    run_id: str
    session_id: str
    job_type: JobType = JobType.DAILY_ARCHIVAL
    timezone: str
    force: bool = False

    report_date: str  # local calendar date, e.g. "2026-07-17"
    report_start_local: datetime
    report_end_local: datetime
    start_utc: datetime
    end_utc: datetime

    audit_regions: list[str] = Field(default_factory=list)

    @classmethod
    def from_request(
        cls,
        request: DailyJobRequest,
        account_id: str,
        now: datetime | None = None,
    ) -> "RunContext":
        """Build a context, computing the previous-local-day report window.

        Implements plan section 9. ``now`` is injectable for testing.
        """
        local_tz = ZoneInfo(request.timezone)
        local_now = (now or datetime.now(timezone.utc)).astimezone(local_tz)

        # windowDaysAgo=1 -> previous local day (default). 0 -> current day so
        # far (end is midnight tonight, in the future; lookup_events returns up
        # to now). N -> the local day N days ago.
        midnight_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        report_start_local = midnight_today - timedelta(days=request.window_days_ago)
        report_end_local = report_start_local + timedelta(days=1)

        start_utc = report_start_local.astimezone(timezone.utc)
        end_utc = report_end_local.astimezone(timezone.utc)

        run_id = request.run_id or generate_run_id(now)
        session_id = request.session_id or generate_session_id(run_id)

        return cls(
            account_id=account_id,
            run_id=run_id,
            session_id=session_id,
            job_type=request.job_type,
            timezone=request.timezone,
            force=request.force,
            report_date=report_start_local.strftime("%Y-%m-%d"),
            report_start_local=report_start_local,
            report_end_local=report_end_local,
            start_utc=start_utc,
            end_utc=end_utc,
            audit_regions=request.effective_regions(),
        )

    def local_window_label(self) -> str:
        return (
            f"{self.report_start_local.isoformat()} "
            f"through {self.report_end_local.isoformat()} ({self.timezone})"
        )

    def utc_window_label(self) -> str:
        return f"{self.start_utc.isoformat()} through {self.end_utc.isoformat()} (UTC)"


class DailyJobResult(BaseModel):
    """Compact result returned to the dispatcher (plan section 7)."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(alias="runId")
    status: RunStatus
    report_date: str | None = Field(default=None, alias="reportDate")
    collection_status: CollectionStatus | None = Field(
        default=None, alias="collectionStatus"
    )
    ai_status: AIStatus | None = Field(default=None, alias="aiStatus")
    events_collected: int = Field(default=0, alias="eventsCollected")
    events_included: int = Field(default=0, alias="eventsIncluded")
    episodes_generated: int = Field(default=0, alias="episodesGenerated")
    report_s3_uri: str | None = Field(default=None, alias="reportS3Uri")
    email_status: EmailStatus | None = Field(default=None, alias="emailStatus")
    fallback_used: bool = Field(default=False, alias="fallbackUsed")
    tools_called: int = Field(default=0, alias="toolsCalled")
    session_id: str | None = Field(default=None, alias="sessionId")
    message: str | None = None

    def to_payload(self) -> dict:
        """Serialize using camelCase aliases for the dispatcher response.

        ``mode="json"`` ensures enums become their string values and datetimes
        become ISO strings, so the result is directly JSON-serializable.
        """
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
