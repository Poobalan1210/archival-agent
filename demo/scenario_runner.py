#!/usr/bin/env python3
"""Create safe, real AWS activity for the Archival Agent demo (plan Phase 7).

Runs a small set of harmless operations so the next scheduled run has genuine
CloudTrail management events to analyze:

  1. Create and tag an SQS queue.
  2. (Optional) Update a demo Lambda's configuration.
  3. Deploy a small successful CloudFormation stack (an SNS topic).
  4. Deploy a stack that intentionally rolls back (duplicate log-group name).
  5. Add a documentation-range ingress rule to an unattached demo security group.

Every resource is prefixed and tagged. State is written to
``demo/.demo_state.json`` so ``cleanup.py`` can remove everything.

Usage:
    python demo/scenario_runner.py [--config demo/setup.yaml] [--dry-run]
                                   [--only sqs_queue,cfn_failure]

Runs locally against real AWS using your default credentials. It is your
responsibility to run this only in an account/region you own.
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
logger = logging.getLogger("demo")

STATE_PATH = Path(__file__).resolve().parent / ".demo_state.json"

SUCCESS_TEMPLATE = json.dumps(
    {
        "Description": "Archival Agent demo - successful stack",
        "Resources": {
            "DemoTopic": {
                "Type": "AWS::SNS::Topic",
                "Properties": {"DisplayName": "archival-agent-demo-topic"},
            }
        },
    }
)

# Two log groups with the same name: the second fails, forcing a rollback.
FAILURE_TEMPLATE = json.dumps(
    {
        "Description": "Archival Agent demo - intentionally failing stack",
        "Resources": {
            "DemoLogGroupA": {
                "Type": "AWS::Logs::LogGroup",
                "Properties": {
                    "LogGroupName": "/archival-agent-demo/duplicate",
                    "RetentionInDays": 1,
                },
            },
            "DemoLogGroupB": {
                "Type": "AWS::Logs::LogGroup",
                "Properties": {
                    "LogGroupName": "/archival-agent-demo/duplicate",
                    "RetentionInDays": 1,
                },
            },
        },
    }
)


class DemoState:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[dict] = []
        if path.exists():
            self.items = json.loads(path.read_text()).get("resources", [])

    def add(self, kind: str, identifier: str, region: str, extra: dict | None = None):
        self.items.append(
            {"type": kind, "id": identifier, "region": region, **(extra or {})}
        )
        self.save()

    def save(self):
        self.path.write_text(json.dumps({"resources": self.items}, indent=2))


def _assert_prefixed(name: str, prefix: str) -> None:
    if not name.startswith(prefix):
        raise ValueError(f"Refusing to create '{name}': must start with '{prefix}'.")


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_sqs_queue(session, cfg, state, dry_run) -> None:
    name = cfg["prefix"] + "queue"
    _assert_prefixed(name, cfg["prefix"])
    logger.info("[sqs_queue] create + tag queue %s", name)
    if dry_run:
        return
    sqs = session.client("sqs")
    resp = sqs.create_queue(
        QueueName=name, tags={cfg["tag_key"]: cfg["tag_value"]}
    )
    url = resp["QueueUrl"]
    # A second tagging call produces an additional management event.
    sqs.tag_queue(QueueUrl=url, Tags={"purpose": "archival-agent-demo"})
    state.add("sqs_queue", url, cfg["region"])
    logger.info("[sqs_queue] created %s", url)


def scenario_lambda_update(session, cfg, state, dry_run) -> None:
    fn = cfg.get("lambda_function_name") or ""
    if not fn:
        logger.info("[lambda_update] no lambda_function_name configured; skipping")
        return
    _assert_prefixed(fn, cfg["prefix"])
    logger.info("[lambda_update] update configuration of %s", fn)
    if dry_run:
        return
    lam = session.client("lambda")
    try:
        current = lam.get_function_configuration(FunctionName=fn)
    except ClientError as exc:
        logger.warning("[lambda_update] %s not found: %s", fn, exc.response["Error"]["Code"])
        return
    new_timeout = 15 if current.get("Timeout") != 15 else 20
    lam.update_function_configuration(FunctionName=fn, Timeout=new_timeout)
    state.add("lambda_config", fn, cfg["region"], {"note": "configuration changed"})
    logger.info("[lambda_update] set timeout=%s on %s", new_timeout, fn)


def _deploy_stack(session, cfg, state, name, template, expect_success, dry_run) -> None:
    _assert_prefixed(name, cfg["prefix"])
    logger.info("[cfn] create stack %s (expect %s)", name, "success" if expect_success else "rollback")
    if dry_run:
        return
    cfn = session.client("cloudformation")
    try:
        cfn.create_stack(
            StackName=name,
            TemplateBody=template,
            Tags=[{"Key": cfg["tag_key"], "Value": cfg["tag_value"]}],
            OnFailure="ROLLBACK",
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "AlreadyExistsException":
            logger.info("[cfn] stack %s already exists", name)
            state.add("cfn_stack", name, cfg["region"])
            return
        raise
    state.add("cfn_stack", name, cfg["region"])
    waiter_name = "stack_create_complete" if expect_success else "stack_rollback_complete"
    try:
        cfn.get_waiter(waiter_name).wait(
            StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 30}
        )
        logger.info("[cfn] stack %s reached expected state", name)
    except WaiterError:
        # For the failing stack a rollback is the expected outcome.
        logger.info("[cfn] stack %s finished (see console for final state)", name)


def scenario_cfn_success(session, cfg, state, dry_run) -> None:
    _deploy_stack(session, cfg, state, cfg["prefix"] + "success", SUCCESS_TEMPLATE, True, dry_run)


def scenario_cfn_failure(session, cfg, state, dry_run) -> None:
    _deploy_stack(session, cfg, state, cfg["prefix"] + "failure", FAILURE_TEMPLATE, False, dry_run)


def scenario_security_group(session, cfg, state, dry_run) -> None:
    name = cfg["prefix"] + "sg"
    _assert_prefixed(name, cfg["prefix"])
    logger.info("[security_group] create SG %s + doc-range ingress %s:%s",
                name, cfg["doc_cidr"], cfg["doc_port"])
    if dry_run:
        return
    ec2 = session.client("ec2")
    vpc_id = cfg.get("vpc_id") or ""
    if not vpc_id:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
        if not vpcs["Vpcs"]:
            logger.warning("[security_group] no default VPC; set vpc_id in config. Skipping.")
            return
        vpc_id = vpcs["Vpcs"][0]["VpcId"]

    resp = ec2.create_security_group(
        GroupName=name,
        Description="Archival Agent demo security group (unattached)",
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [{"Key": cfg["tag_key"], "Value": cfg["tag_value"]}],
            }
        ],
    )
    group_id = resp["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": cfg["doc_port"],
                "ToPort": cfg["doc_port"],
                "IpRanges": [
                    {"CidrIp": cfg["doc_cidr"], "Description": "archival-agent-demo doc range"}
                ],
            }
        ],
    )
    state.add("security_group", group_id, cfg["region"])
    logger.info("[security_group] created %s in %s", group_id, vpc_id)


def scenario_network_change(session, cfg, state, dry_run) -> None:
    """Add a real ingress rule (SUCCESS), then re-add it (a deliberate duplicate
    that FAILS). Produces CloudTrail-visible success + failure network changes
    for the analyst to distinguish and flag.
    """
    name = cfg["prefix"] + "sg"
    logger.info("[network_change] add + duplicate ingress on %s", name)
    if dry_run:
        return
    ec2 = session.client("ec2")
    groups = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}]
    ).get("SecurityGroups", [])
    if not groups:
        logger.warning("[network_change] SG %s not found; run security_group first.", name)
        return
    gid = groups[0]["GroupId"]
    perm = [
        {
            "IpProtocol": "tcp",
            "FromPort": 8443,
            "ToPort": 8443,
            "IpRanges": [{"CidrIp": cfg["doc_cidr"], "Description": "archival-agent-demo mgmt port"}],
        }
    ]
    try:
        ec2.authorize_security_group_ingress(GroupId=gid, IpPermissions=perm)
        logger.info("[network_change] added ingress tcp/8443 (success)")
    except ClientError as exc:
        logger.info("[network_change] add ingress: %s", exc.response["Error"]["Code"])
    # Deliberate duplicate -> InvalidPermission.Duplicate: a safe, real failure.
    try:
        ec2.authorize_security_group_ingress(GroupId=gid, IpPermissions=perm)
        logger.info("[network_change] duplicate unexpectedly succeeded")
    except ClientError as exc:
        logger.info("[network_change] duplicate failed as expected: %s", exc.response["Error"]["Code"])


def scenario_sqs_modify(session, cfg, state, dry_run) -> None:
    """Change the demo queue's configuration (a resource modification event)."""
    qname = cfg["prefix"] + "queue"
    logger.info("[sqs_modify] set attributes on %s", qname)
    if dry_run:
        return
    sqs = session.client("sqs")
    try:
        url = sqs.get_queue_url(QueueName=qname)["QueueUrl"]
    except ClientError as exc:
        logger.warning("[sqs_modify] queue %s not found: %s", qname, exc.response["Error"]["Code"])
        return
    sqs.set_queue_attributes(
        QueueUrl=url,
        Attributes={"MessageRetentionPeriod": "1209600", "VisibilityTimeout": "45"},
    )
    logger.info("[sqs_modify] updated retention + visibility timeout")


SCENARIOS = {
    "sqs_queue": scenario_sqs_queue,
    "lambda_update": scenario_lambda_update,
    "cfn_success": scenario_cfn_success,
    "cfn_failure": scenario_cfn_failure,
    "security_group": scenario_security_group,
    "network_change": scenario_network_change,
    "sqs_modify": scenario_sqs_modify,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Archival Agent demo scenarios.")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "setup.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="Print actions without calling AWS.")
    parser.add_argument("--only", default="", help="Comma-separated subset of scenarios to run.")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    session = boto3.Session(region_name=cfg["region"])
    state = DemoState(STATE_PATH)

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    enabled = cfg.get("scenarios", {})

    logger.info("Region: %s | prefix: %s | dry-run: %s", cfg["region"], cfg["prefix"], args.dry_run)
    ran = 0
    for name, func in SCENARIOS.items():
        if only and name not in only:
            continue
        if not only and not enabled.get(name, False):
            logger.info("[%s] disabled in config; skipping", name)
            continue
        try:
            func(session, cfg, state, args.dry_run)
            ran += 1
        except Exception:  # noqa: BLE001 - one scenario failing must not abort the rest
            logger.exception("[%s] failed", name)

    logger.info("Done. Ran %d scenario(s). State: %s", ran, STATE_PATH if not args.dry_run else "(dry-run)")
    logger.info("Remember to run cleanup.py after the Archival Agent has reported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
