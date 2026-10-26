#!/usr/bin/env python3
"""Render the summary email for a stored report.json, for previews and screenshots.

Usage: python demo/render_email.py demo/out/after.json demo/out/after_email.html
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.models.diagnosis import AccessDiagnosis  # noqa: E402
from agent.models.report import ArchivalReport  # noqa: E402
from agent.renderer import render_email_html  # noqa: E402


def main(src: str, dest: str) -> None:
    data = json.loads(Path(src).read_text())
    coverage = data["collection"]
    html = render_email_html({
        "generated_at": data["generatedAt"],
        "meta": data["meta"],
        "coverage": {**coverage, "partial": bool(coverage.get("failed_regions"))},
        "totals": data["totals"],
        "report": ArchivalReport.model_validate(data["report"]),
        "agent_execution": data["agentExecution"],
        "access_diagnoses": [AccessDiagnosis.model_validate(d) for d in data.get("accessDiagnoses", [])],
        "access_summary": data.get("accessSummary") or {},
        "presigned_url": "#",
        "now": datetime.now(),
    })
    Path(dest).write_text(html)


if __name__ == "__main__":
    main(*sys.argv[1:3])
