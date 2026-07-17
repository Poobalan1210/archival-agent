#!/usr/bin/env python3
"""Create or update the Archival Agent AgentCore Runtime (plan Phase 2).

Registers a container image (already pushed to ECR) as an AgentCore Runtime,
wiring in the execution role and the runtime environment variables the app
reads (SES identities, model ID, region, bucket).

Usage (normally called by deploy/deploy_runtime.sh):
    IMAGE_URI=<ecr-uri>:latest \\
    RUNTIME_ROLE_ARN=<RuntimeExecutionRoleArn from the SAM stack> \\
    AGENT_REGION=us-east-1 \\
    SES_SENDER=you@example.com SES_RECIPIENT=ops@example.com \\
    python deploy/create_runtime.py

Prints the runtime ARN and writes it to deploy/.runtime_arn so it can be fed
back into the SAM stack's AgentRuntimeArn parameter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

RUNTIME_NAME = os.environ.get("RUNTIME_NAME", "archival_agent")
ARN_OUT = Path(__file__).resolve().parent / ".runtime_arn"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"ERROR: environment variable {name} is required.")
    return value


def _runtime_env(account_id: str, region: str) -> dict[str, str]:
    env = {
        "AWS_ACCOUNT_ID": account_id,
        "AGENT_REGION": region,
        "NOVA_MODEL_ID": os.environ.get("NOVA_MODEL_ID", "amazon.nova-pro-v1:0"),
        "AGENT_OBSERVABILITY_ENABLED": "true",
    }
    for optional in ("REPORT_BUCKET", "SES_SENDER", "SES_RECIPIENT"):
        value = os.environ.get(optional, "").strip()
        if value:
            env[optional] = value
    return env


def _find_runtime_id(client, name: str) -> str | None:
    paginator = client.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime.get("agentRuntimeName") == name:
                return runtime.get("agentRuntimeId")
    return None


def main() -> int:
    region = os.environ.get("AGENT_REGION", "us-east-1").strip() or "us-east-1"
    image_uri = _require("IMAGE_URI")
    role_arn = _require("RUNTIME_ROLE_ARN")

    account_id = os.environ.get("AWS_ACCOUNT_ID", "").strip()
    if not account_id:
        account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]

    client = boto3.client("bedrock-agentcore-control", region_name=region)

    common = {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": _runtime_env(account_id, region),
    }

    existing_id = _find_runtime_id(client, RUNTIME_NAME)
    try:
        if existing_id:
            print(f"Updating existing runtime {RUNTIME_NAME} ({existing_id})...")
            response = client.update_agent_runtime(agentRuntimeId=existing_id, **common)
        else:
            print(f"Creating runtime {RUNTIME_NAME}...")
            response = client.create_agent_runtime(agentRuntimeName=RUNTIME_NAME, **common)
    except ClientError as exc:
        sys.exit(f"ERROR: {exc.response['Error'].get('Code')}: {exc.response['Error'].get('Message')}")

    arn = response.get("agentRuntimeArn", "")
    status = response.get("status", "UNKNOWN")
    ARN_OUT.write_text(arn)
    print(f"\nRuntime ARN: {arn}")
    print(f"Status: {status}")
    print(f"(written to {ARN_OUT})")
    print(
        "\nNext: update the SAM stack so the dispatcher can invoke it:\n"
        f"  sam deploy --parameter-overrides AgentRuntimeArn={arn} ...\n"
        "Then smoke-test with a health payload before enabling the schedule."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
