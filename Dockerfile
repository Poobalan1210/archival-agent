# Archival Agent - AgentCore Runtime container.
#
# AgentCore requires linux/arm64. BedrockAgentCoreApp (in agent/app.py) provides
# the mandatory /invocations POST and /ping GET endpoints on port 8080. The app
# is launched under ADOT auto-instrumentation for AgentCore Observability.
#
# Build + push (ARM64) with deploy/deploy_runtime.sh, or:
#   docker buildx build --platform linux/arm64 -t <ecr-uri>:latest --push .
FROM --platform=linux/arm64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY agent/requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

# Copy the application package and the data files it reads at runtime.
# config.py resolves rules/prompts/templates relative to the repo root (/app).
COPY agent/ /app/agent/
COPY prompts/ /app/prompts/
COPY rules/ /app/rules/
COPY templates/ /app/templates/

# Default configuration. Override per-deployment via the runtime's
# environmentVariables (see deploy/create_runtime.py).
ENV AGENT_REGION=us-east-1 \
    NOVA_MODEL_ID=amazon.nova-pro-v1:0 \
    AGENT_OBSERVABILITY_ENABLED=true

EXPOSE 8080

# ADOT auto-instrumentation -> Strands emits OTEL traces to CloudWatch.
CMD ["opentelemetry-instrument", "python", "-m", "agent.app"]
