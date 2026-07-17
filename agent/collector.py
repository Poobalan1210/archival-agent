"""CloudTrail event collection across regions (plan section 10).

For each configured region we page through ``lookup_events`` for the report
window. A failure in one region degrades the run to PARTIAL rather than
aborting it. The client factory is injectable so the pipeline can be tested
without live AWS calls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from agent.config import Config
from agent.models.request import CollectionCoverage, RegionFailure, RunContext

logger = logging.getLogger(__name__)

ClientFactory = Callable[[str], Any]


def _default_client_factory(region: str) -> Any:
    return boto3.client(
        "cloudtrail",
        region_name=region,
        config=BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _collect_region(
    client: Any,
    context: RunContext,
    page_size: int,
    max_events: int,
) -> list[dict]:
    """Page through lookup_events for a single region."""
    events: list[dict] = []
    next_token: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "StartTime": context.start_utc,
            "EndTime": context.end_utc,
            "MaxResults": page_size,
        }
        if next_token:
            kwargs["NextToken"] = next_token

        response = client.lookup_events(**kwargs)
        page = response.get("Events", [])
        events.extend(page)

        if len(events) >= max_events:
            logger.warning(
                "Region cap of %d events reached; truncating collection", max_events
            )
            events = events[:max_events]
            break

        next_token = response.get("NextToken")
        if not next_token:
            break

    return events


def collect_events(
    context: RunContext,
    config: Config,
    client_factory: ClientFactory | None = None,
) -> tuple[list[dict], CollectionCoverage]:
    """Collect raw CloudTrail events for every audit region.

    Returns the combined raw events and a :class:`CollectionCoverage` describing
    which regions succeeded or failed.
    """
    factory = client_factory or _default_client_factory
    coverage = CollectionCoverage(requested_regions=list(context.audit_regions))
    all_events: list[dict] = []

    for region in context.audit_regions:
        try:
            client = factory(region)
            region_events = _collect_region(
                client,
                context,
                page_size=config.lookup_page_size,
                max_events=config.max_events_per_region,
            )
            all_events.extend(region_events)
            coverage.completed_regions.append(region)
            logger.info("Collected %d events from %s", len(region_events), region)
        except (ClientError, BotoCoreError) as exc:
            error_name = type(exc).__name__
            if isinstance(exc, ClientError):
                error_name = exc.response.get("Error", {}).get("Code", error_name)
            logger.error("Region %s failed: %s", region, error_name)
            coverage.failed_regions.append(RegionFailure(region=region, error=error_name))
        except Exception as exc:  # noqa: BLE001 - defensive; keep other regions alive
            logger.exception("Unexpected error collecting region %s", region)
            coverage.failed_regions.append(
                RegionFailure(region=region, error=type(exc).__name__)
            )

    coverage.finalize()
    logger.info(
        "Collection %s: %d events across %d/%d regions",
        coverage.status,
        len(all_events),
        len(coverage.completed_regions),
        len(coverage.requested_regions),
    )
    return all_events, coverage
