#!/usr/bin/env python3
"""Remove all Archival Agent demo resources (plan Phase 7 cleanup).

Deletes resources recorded in ``demo/.demo_state.json`` by ``scenario_runner``.
With ``--discover`` it also finds prefixed resources not present in state (for
example if the state file was lost). Every deletion is guarded: a resource is
only removed if its name starts with the configured prefix or it carries the
demo tag.

Usage:
    python demo/cleanup.py [--config demo/setup.yaml] [--dry-run] [--discover]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError, WaiterError

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("cleanup")

STATE_PATH = Path(__file__).resolve().parent / ".demo_state.json"


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def load_state() -> list[dict]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text()).get("resources", [])
    return []


def _guard(name: str, prefix: str) -> bool:
    if name and name.startswith(prefix):
        return True
    logger.warning("Refusing to delete '%s' (does not start with '%s')", name, prefix)
    return False


def delete_sqs(session, cfg, url, dry_run) -> None:
    name = url.rsplit("/", 1)[-1]
    if not _guard(name, cfg["prefix"]):
        return
    logger.info("[sqs] delete %s", name)
    if dry_run:
        return
    try:
        session.client("sqs").delete_queue(QueueUrl=url)
    except ClientError as exc:
        logger.warning("[sqs] %s: %s", name, exc.response["Error"]["Code"])


def delete_stack(session, cfg, name, dry_run) -> None:
    if not _guard(name, cfg["prefix"]):
        return
    logger.info("[cfn] delete stack %s", name)
    if dry_run:
        return
    cfn = session.client("cloudformation")
    try:
        cfn.delete_stack(StackName=name)
        cfn.get_waiter("stack_delete_complete").wait(
            StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 30}
        )
        logger.info("[cfn] deleted %s", name)
    except WaiterError:
        logger.info("[cfn] delete of %s finished (verify in console)", name)
    except ClientError as exc:
        logger.warning("[cfn] %s: %s", name, exc.response["Error"]["Code"])


def delete_security_group(session, cfg, group_id, dry_run) -> None:
    ec2 = session.client("ec2")
    # Verify the group is really ours before deleting (id has no prefix).
    try:
        groups = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"]
    except ClientError as exc:
        logger.warning("[sg] %s: %s", group_id, exc.response["Error"]["Code"])
        return
    if not groups:
        return
    group = groups[0]
    tags = {t["Key"]: t["Value"] for t in group.get("Tags", [])}
    is_ours = group["GroupName"].startswith(cfg["prefix"]) or tags.get(cfg["tag_key"]) == cfg["tag_value"]
    if not is_ours:
        logger.warning("Refusing to delete SG %s (%s): not a demo resource", group_id, group["GroupName"])
        return
    logger.info("[sg] delete %s (%s)", group_id, group["GroupName"])
    if dry_run:
        return
    try:
        ec2.delete_security_group(GroupId=group_id)
    except ClientError as exc:
        logger.warning("[sg] %s: %s", group_id, exc.response["Error"]["Code"])


def discover(session, cfg) -> list[dict]:
    """Find prefixed demo resources not necessarily recorded in state."""
    found: list[dict] = []
    prefix = cfg["prefix"]
    try:
        urls = session.client("sqs").list_queues(QueueNamePrefix=prefix).get("QueueUrls", [])
        found += [{"type": "sqs_queue", "id": u, "region": cfg["region"]} for u in urls]
    except ClientError:
        logger.warning("[discover] sqs list failed", exc_info=True)
    try:
        ec2 = session.client("ec2")
        groups = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [prefix + "*"]}]
        )["SecurityGroups"]
        found += [{"type": "security_group", "id": g["GroupId"], "region": cfg["region"]} for g in groups]
    except ClientError:
        logger.warning("[discover] ec2 list failed", exc_info=True)
    try:
        cfn = session.client("cloudformation")
        for page in cfn.get_paginator("list_stacks").paginate(
            StackStatusFilter=[
                "CREATE_COMPLETE", "ROLLBACK_COMPLETE", "CREATE_FAILED",
                "ROLLBACK_FAILED", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
            ]
        ):
            for s in page["StackSummaries"]:
                if s["StackName"].startswith(prefix):
                    found.append({"type": "cfn_stack", "id": s["StackName"], "region": cfg["region"]})
    except ClientError:
        logger.warning("[discover] cfn list failed", exc_info=True)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean up Archival Agent demo resources.")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "setup.yaml"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover", action="store_true", help="Also find prefixed resources not in state.")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    session = boto3.Session(region_name=cfg["region"])

    resources = load_state()
    if args.discover:
        seen = {(r["type"], r["id"]) for r in resources}
        for item in discover(session, cfg):
            if (item["type"], item["id"]) not in seen:
                resources.append(item)

    if not resources:
        logger.info("Nothing to clean up.")
        return 0

    # Delete queues/SGs first, then stacks last (stacks may own other things).
    order = {"sqs_queue": 0, "security_group": 1, "lambda_config": 2, "cfn_stack": 3}
    resources.sort(key=lambda r: order.get(r["type"], 9))

    for res in resources:
        kind = res["type"]
        if kind == "sqs_queue":
            delete_sqs(session, cfg, res["id"], args.dry_run)
        elif kind == "cfn_stack":
            delete_stack(session, cfg, res["id"], args.dry_run)
        elif kind == "security_group":
            delete_security_group(session, cfg, res["id"], args.dry_run)
        elif kind == "lambda_config":
            logger.info("[lambda] configuration change on %s left as-is (not a created resource)", res["id"])
        else:
            logger.warning("Unknown resource type: %s", kind)

    if not args.dry_run:
        STATE_PATH.unlink(missing_ok=True)
        logger.info("Cleared %s", STATE_PATH)
    logger.info("Cleanup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
