"""Central configuration for the Archival Agent runtime.

All configuration is sourced from environment variables so the same code runs
locally, in the AgentCore Runtime, and in tests. Nothing here performs network
calls; it only resolves and validates configuration values.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Region that always hosts the agent, Nova Pro inference, S3, SES and the
#: dispatcher. See plan section 5.
AGENT_REGION = "us-east-1"

#: Global-service events (IAM, CloudFront, Route 53, ...) are recorded in
#: us-east-1, so the collector always includes it regardless of user config.
GLOBAL_SERVICE_REGION = "us-east-1"

#: Default foundation model. Overridable via ``NOVA_MODEL_ID``.
DEFAULT_NOVA_MODEL_ID = "amazon.nova-pro-v1:0"

#: Directory that ships the deterministic rule files (rules/*.yaml).
_REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = _REPO_ROOT / "rules"
PROMPTS_DIR = _REPO_ROOT / "prompts"
TEMPLATES_DIR = _REPO_ROOT / "templates"

#: S3 key layout. See plan section 20.
S3_PREFIX_TEMPLATE = "account={account_id}/date={report_date}/run={run_id}/"
S3_COMPLETED_MARKER_TEMPLATE = "account={account_id}/date={report_date}/completed.json"
S3_LOCK_TEMPLATE = "account={account_id}/date={report_date}/lock.json"


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration."""

    # Identity / placement
    account_id: str
    agent_region: str = AGENT_REGION

    # Model
    nova_model_id: str = DEFAULT_NOVA_MODEL_ID
    model_temperature: float = 0.2
    model_max_tokens: int = 4096

    # Storage
    report_bucket: str = ""

    # Email (SES)
    ses_sender: str = ""
    ses_recipient: str = ""
    presigned_url_ttl_seconds: int = 7 * 24 * 3600

    # Collection limits
    lookup_page_size: int = 50
    max_events_per_region: int = 5000
    max_tool_records: int = 50

    # Max episodes shown to the agent's index. The agent investigates only the
    # most significant episodes (failed / security / IAM / network first) to
    # keep tool use and output bounded. Deterministic totals still cover ALL
    # episodes. Prevents MaxTokensReached on high-volume days.
    max_agent_episodes: int = 25

    # Behaviour flags
    dry_run: bool = False

    # Loaded rule documents (populated lazily)
    _rules_cache: dict = field(default_factory=dict, compare=False, repr=False)

    # -- Rule loaders ------------------------------------------------------

    def load_rules(self, name: str) -> dict:
        """Load and cache a YAML rule document from the rules/ directory."""
        if name in self._rules_cache:
            return self._rules_cache[name]
        path = RULES_DIR / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Rule file not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}
        self._rules_cache[name] = document
        return document

    @property
    def event_categories(self) -> dict:
        return self.load_rules("event_categories")

    @property
    def request_allowlist(self) -> dict:
        return self.load_rules("request_allowlist")

    @property
    def redaction_fields(self) -> dict:
        return self.load_rules("redaction_fields")

    @property
    def recommendation_rules(self) -> dict:
        return self.load_rules("recommendation_rules")

    # -- Derived helpers ---------------------------------------------------

    def default_report_bucket(self) -> str:
        """S3 bucket name, defaulting to the account-scoped convention."""
        if self.report_bucket:
            return self.report_bucket
        return f"archival-agent-{self.account_id}"

    def s3_run_prefix(self, report_date: str, run_id: str) -> str:
        return S3_PREFIX_TEMPLATE.format(
            account_id=self.account_id,
            report_date=report_date,
            run_id=run_id,
        )

    def s3_completed_marker(self, report_date: str) -> str:
        return S3_COMPLETED_MARKER_TEMPLATE.format(
            account_id=self.account_id,
            report_date=report_date,
        )

    def s3_lock_key(self, report_date: str) -> str:
        return S3_LOCK_TEMPLATE.format(
            account_id=self.account_id,
            report_date=report_date,
        )


@functools.lru_cache(maxsize=1)
def load_config() -> Config:
    """Build a :class:`Config` from the current environment.

    Cached so repeated calls within a single runtime invocation are cheap.
    Call ``load_config.cache_clear()`` in tests when mutating the environment.
    """

    account_id = os.environ.get("AWS_ACCOUNT_ID", "").strip()

    return Config(
        account_id=account_id,
        agent_region=os.environ.get("AGENT_REGION", AGENT_REGION).strip() or AGENT_REGION,
        nova_model_id=os.environ.get("NOVA_MODEL_ID", DEFAULT_NOVA_MODEL_ID).strip()
        or DEFAULT_NOVA_MODEL_ID,
        model_temperature=float(os.environ.get("NOVA_TEMPERATURE", "0.2") or "0.2"),
        model_max_tokens=_get_int("NOVA_MAX_TOKENS", 5120),
        report_bucket=os.environ.get("REPORT_BUCKET", "").strip(),
        ses_sender=os.environ.get("SES_SENDER", "").strip(),
        ses_recipient=os.environ.get("SES_RECIPIENT", "").strip(),
        presigned_url_ttl_seconds=_get_int("PRESIGNED_URL_TTL_SECONDS", 7 * 24 * 3600),
        lookup_page_size=_get_int("LOOKUP_PAGE_SIZE", 50),
        max_events_per_region=_get_int("MAX_EVENTS_PER_REGION", 5000),
        max_tool_records=_get_int("MAX_TOOL_RECORDS", 20),
        max_agent_episodes=_get_int("MAX_AGENT_EPISODES", 25),
        dry_run=_get_bool("DRY_RUN", False),
    )
