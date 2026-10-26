#!/usr/bin/env python3
"""Run the same real day twice, without and with diagnose_access_denied, and compare.

Both runs collect the same CloudTrail window, use the same model and validator,
and differ only in whether the agent has the new tool. Reports go to a separate
bucket (REPORT_BUCKET) with email disabled, so your real history is untouched.

Usage:
    REPORT_BUCKET=my-demo-bucket AWS_ACCOUNT_ID=123456789012 \\
      python demo/before_after.py [--days-ago 1] [--timezone Asia/Kolkata] [--regions ap-south-1,us-east-1]

Writes demo/out/{before,after}.{json,html} and prints a scorecard.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DRY_RUN", "true")

import boto3  # noqa: E402

from agent.app import run_archival  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.models.request import DailyJobRequest  # noqa: E402

OUT = ROOT / "demo" / "out"
VAGUE = re.compile(r"not (?:been )?identified|not determined|review (?:the )?iam polic", re.I)


def run(label: str, enabled: bool, request: DailyJobRequest) -> dict:
    config = dataclasses.replace(load_config(), access_diagnosis_enabled=enabled)
    result = run_archival(request, config)
    if not result.report_s3_uri:
        raise SystemExit(f"{label} run failed: {result.status} {result.message}")
    bucket, key = result.report_s3_uri.removeprefix("s3://").split("/", 1)
    prefix = key.rsplit("/", 1)[0] + "/"
    s3 = boto3.client("s3")
    OUT.mkdir(parents=True, exist_ok=True)
    report = json.loads(s3.get_object(Bucket=bucket, Key=prefix + "report.json")["Body"].read())
    (OUT / f"{label}.json").write_text(json.dumps(report, indent=2))
    (OUT / f"{label}.html").write_bytes(s3.get_object(Bucket=bucket, Key=prefix + "report.html")["Body"].read())
    return report


def score(report: dict) -> dict:
    body = report["report"]
    text = json.dumps(body)
    access = report.get("accessSummary") or {}
    return {
        "AccessDenied events": access.get("deniedEvents", "-"),
        "Root causes identified": access.get("diagnoses", 0),
        "Denied events explained": access.get("eventsExplained", 0),
        "Fixes verified by IAM simulator": access.get("verifiedFixes", 0),
        "Recommendations citing a diagnosis": sum(bool(re.search(r"\bAD-\d+", json.dumps(r)))
                                                  for r in body["recommendations"]),
        "'not identified' / 'review IAM policies'": len(VAGUE.findall(text)),
        "AI status": report["agentExecution"]["aiStatus"],
        "Tools called": report["agentExecution"]["toolsCalled"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days-ago", type=int, default=1)
    parser.add_argument("--timezone", default="Asia/Kolkata")
    parser.add_argument("--regions", default="ap-south-1,us-east-1")
    parser.add_argument("--before-report", help="score a stored report.json as BEFORE instead of re-running it, "
                                                "e.g. the report the deployed agent produced for that day")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    request = DailyJobRequest(windowDaysAgo=args.days_ago, timezone=args.timezone,
                              auditRegions=args.regions.split(","), force=True)
    if args.before_report:
        before = score(json.loads(Path(args.before_report).read_text()))
    else:
        before = score(run("before", False, request))
    after = score(run("after", True, request))

    print(f"\n{'':44}{'BEFORE':>10}{'AFTER':>10}")
    for k in before:
        print(f"{k:44}{str(before[k]):>10}{str(after[k]):>10}")
    print(f"\nReports: {OUT}/before.html  {OUT}/after.html")


if __name__ == "__main__":
    main()
