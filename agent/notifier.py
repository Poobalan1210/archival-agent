"""SES email notification (plan sections 19 and 22).

Sends the HTML summary email. If SES identities are not configured, the send is
skipped (not an error) so the run can still complete and store its report.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from agent.config import Config
from agent.models.enums import EmailStatus
from agent.models.report import ArchivalReport
from agent.models.request import CollectionCoverage

logger = logging.getLogger(__name__)


def build_subject(
    report: ArchivalReport, coverage: CollectionCoverage, report_date: str, failures: int
) -> str:
    prefix = "[PARTIAL] " if coverage.failed_regions else ""
    flag = f" — {failures} failed" if failures else ""
    return f"{prefix}Archival Agent — {report_date}{flag}"


def _plain_text(report: ArchivalReport) -> str:
    lines = [report.headline, ""]
    lines.extend(f"- {item}" for item in report.executive_summary)
    if report.recommendations:
        lines.append("")
        lines.append("Top recommendations:")
        for rec in report.recommendations[:3]:
            lines.append(f"- [{rec.priority}] {rec.title}: {rec.suggested_next_step}")
    lines.append("")
    lines.append("The detailed report is stored privately in Amazon S3.")
    return "\n".join(lines)


def send_email(
    config: Config,
    subject: str,
    html_body: str,
    report: ArchivalReport,
    client: Any | None = None,
) -> EmailStatus:
    """Send the summary email via SES. Returns an :class:`EmailStatus`."""
    if config.dry_run:
        logger.info("DRY_RUN set; skipping SES send")
        return EmailStatus.SKIPPED
    if not config.ses_sender or not config.ses_recipient:
        logger.warning("SES sender/recipient not configured; skipping email")
        return EmailStatus.SKIPPED

    ses = client or boto3.client("ses", region_name=config.agent_region)
    try:
        ses.send_email(
            Source=config.ses_sender,
            Destination={"ToAddresses": [config.ses_recipient]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": _plain_text(report), "Charset": "UTF-8"},
                },
            },
        )
        logger.info("SES email sent to configured recipient")
        return EmailStatus.SENT
    except (ClientError, BotoCoreError):
        logger.exception("SES send failed")
        return EmailStatus.FAILED
