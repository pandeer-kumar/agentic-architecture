"""Settings, loaded from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import os as _os
REPO_ROOT = Path(_os.environ.get("ITSM_REPO_ROOT") or Path(__file__).resolve().parents[2])


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("ITSM_DATABASE_URL", "postgresql://postgres@localhost:5432/itsm"))
    # "fixture" = built-in fake vendor APIs (no credentials needed); "live" = real vendor APIs
    connector_mode: str = field(default_factory=lambda: _env("ITSM_CONNECTOR_MODE", "fixture"))
    # Claude API: the SDK reads ANTHROPIC_API_KEY itself; base URL only overridden for the fake server in tests
    anthropic_base_url: str | None = field(default_factory=lambda: _env("ITSM_ANTHROPIC_BASE_URL"))
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ITSM_ANTHROPIC_API_KEY"))
    server_side_fallback: bool = field(default_factory=lambda: _env("ITSM_SERVER_SIDE_FALLBACK", "true") == "true")
    strict_tools: bool = field(default_factory=lambda: _env("ITSM_STRICT_TOOLS", "true") == "true")
    # vendor endpoints (live mode)
    jira_base_url: str | None = field(default_factory=lambda: _env("JIRA_BASE_URL"))
    jira_email: str | None = field(default_factory=lambda: _env("JIRA_EMAIL"))
    jira_api_token: str | None = field(default_factory=lambda: _env("JIRA_API_TOKEN"))
    jira_timezone: str = field(default_factory=lambda: _env("JIRA_TIMEZONE", "UTC"))
    jira_cab_field: str = field(default_factory=lambda: _env("JIRA_CAB_FIELD", "customfield_10050"))
    jira_linked_change_field: str = field(default_factory=lambda: _env("JIRA_LINKED_CHANGE_FIELD", "customfield_10051"))
    servicenow_base_url: str | None = field(default_factory=lambda: _env("SERVICENOW_BASE_URL"))
    servicenow_user: str | None = field(default_factory=lambda: _env("SERVICENOW_USER"))
    servicenow_password: str | None = field(default_factory=lambda: _env("SERVICENOW_PASSWORD"))
    servicenow_timezone: str = field(default_factory=lambda: _env("SERVICENOW_TIMEZONE", "UTC"))
    zendesk_base_url: str | None = field(default_factory=lambda: _env("ZENDESK_BASE_URL"))
    zendesk_email: str | None = field(default_factory=lambda: _env("ZENDESK_EMAIL"))
    zendesk_api_token: str | None = field(default_factory=lambda: _env("ZENDESK_API_TOKEN"))
    # pin "now" (ISO-8601) for reproducible demos/tests; empty = real clock
    fixed_now: str | None = field(default_factory=lambda: _env("ITSM_FIXED_NOW"))
    # limits (docs/01 §3, docs/04 §2)
    max_records: int = 2000
    orchestrator_budget_tokens: int = 120_000
    working_set_budget_tokens: int = 55_000
    epoch_turn_cap: int = 40
    map_batch_size: int = 25
    map_concurrency: int = 8
    offload_rows: int = 20
    offload_tokens: int = 4_000
    max_inline_tokens: int = 25_000
    max_revisions: int = 2

    @property
    def model_routing(self) -> dict:
        return yaml.safe_load((REPO_ROOT / "contracts/policies/model_routing.yaml").read_text())

    @property
    def risk_policy(self) -> dict:
        return yaml.safe_load((REPO_ROOT / "contracts/policies/tool_risk_policy.yaml").read_text())


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(s: Settings) -> None:
    global _settings
    _settings = s
