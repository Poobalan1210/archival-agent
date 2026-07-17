#!/usr/bin/env bash
#
# Build the Archival Agent ARM64 image, push it to ECR, and register it as an
# AgentCore Runtime. Run from the repository root after `sam deploy` has created
# the RuntimeExecutionRole.
#
# Required:
#   RUNTIME_ROLE_ARN   RuntimeExecutionRoleArn output from the SAM stack.
# Optional (with defaults):
#   AGENT_REGION       Region for ECR + runtime (default us-east-1).
#   ECR_REPO           ECR repository name (default archival-agent).
#   IMAGE_TAG          Image tag (default latest).
#   SES_SENDER / SES_RECIPIENT / REPORT_BUCKET / NOVA_MODEL_ID  Runtime env vars.
#
# Example:
#   RUNTIME_ROLE_ARN=arn:aws:iam::111122223333:role/archival-agent-runtime-us-east-1 \
#   SES_SENDER=you@example.com SES_RECIPIENT=you@example.com \
#   ./deploy/deploy_runtime.sh
set -euo pipefail

REGION="${AGENT_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-archival-agent}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
: "${RUNTIME_ROLE_ARN:?Set RUNTIME_ROLE_ARN to the SAM RuntimeExecutionRoleArn output}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE_URI="${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"

# Prefer the project venv's python for the boto3 registration step.
PYTHON="python3"
[ -x "${REPO_ROOT}/.venv/bin/python" ] && PYTHON="${REPO_ROOT}/.venv/bin/python"

echo "==> Account ${ACCOUNT_ID} | region ${REGION} | image ${IMAGE_URI}"

echo "==> Ensuring ECR repository exists"
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$ECR_REPO" \
       --image-scanning-configuration scanOnPush=true --region "$REGION" >/dev/null

echo "==> Logging in to ECR"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

echo "==> Building and pushing ARM64 image"
docker buildx inspect archival-builder >/dev/null 2>&1 \
  || docker buildx create --name archival-builder --use
docker buildx use archival-builder
docker buildx build --platform linux/arm64 -t "$IMAGE_URI" --push .

echo "==> Registering AgentCore Runtime"
IMAGE_URI="$IMAGE_URI" \
RUNTIME_ROLE_ARN="$RUNTIME_ROLE_ARN" \
AGENT_REGION="$REGION" \
AWS_ACCOUNT_ID="$ACCOUNT_ID" \
NOVA_MODEL_ID="${NOVA_MODEL_ID:-amazon.nova-pro-v1:0}" \
SES_SENDER="${SES_SENDER:-}" \
SES_RECIPIENT="${SES_RECIPIENT:-}" \
REPORT_BUCKET="${REPORT_BUCKET:-}" \
  "$PYTHON" deploy/create_runtime.py

echo "==> Done."
