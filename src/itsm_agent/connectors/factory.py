"""Builds connectors for 'live' (real vendor APIs) or 'fixture' (built-in fake APIs) mode."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..config import get_settings
from .base import ConnectorError
from .jira import JiraConnector
from .servicenow import ServiceNowConnector
from .zendesk import ZendeskConnector

_fake = None


def fake_vendors(reset: bool = False, **kw):
    """The process-wide fake vendor API (fixture mode)."""
    global _fake
    if _fake is None or reset or kw:
        from ..testing.fake_vendors import FakeVendors, generate
        fixed = get_settings().fixed_now
        now = kw.pop("now", None) or (datetime.fromisoformat(fixed) if fixed else datetime.now(timezone.utc).replace(second=0, microsecond=0))
        gen_kw = {k: kw.pop(k) for k in list(kw) if k in ("n_sn", "n_jira", "n_zendesk", "seed", "link_rate")}
        _fake = FakeVendors(generate(now, **gen_kw), **kw)
    return _fake


def _client(base_url: str, auth=None) -> httpx.Client:
    s = get_settings()
    if s.connector_mode == "fixture":
        return httpx.Client(transport=httpx.MockTransport(fake_vendors().handle), timeout=30)
    return httpx.Client(auth=auth, timeout=httpx.Timeout(60, connect=10), headers={"Accept": "application/json"})


def jira() -> JiraConnector:
    s = get_settings()
    if s.connector_mode == "fixture":
        return JiraConnector(_client("https://jira.fixture"), "https://jira.fixture")
    if not (s.jira_base_url and s.jira_email and s.jira_api_token):
        raise ConnectorError("Jira is not configured: set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN")
    return JiraConnector(_client(s.jira_base_url, (s.jira_email, s.jira_api_token)), s.jira_base_url,
                         s.jira_timezone, s.jira_cab_field, s.jira_linked_change_field)


def servicenow() -> ServiceNowConnector:
    s = get_settings()
    if s.connector_mode == "fixture":
        return ServiceNowConnector(_client("https://servicenow.fixture"), "https://servicenow.fixture")
    if not (s.servicenow_base_url and s.servicenow_user and s.servicenow_password):
        raise ConnectorError("ServiceNow is not configured: set SERVICENOW_BASE_URL, SERVICENOW_USER, SERVICENOW_PASSWORD")
    return ServiceNowConnector(_client(s.servicenow_base_url, (s.servicenow_user, s.servicenow_password)),
                               s.servicenow_base_url, s.servicenow_timezone)


def zendesk() -> ZendeskConnector:
    s = get_settings()
    if s.connector_mode == "fixture":
        return ZendeskConnector(_client("https://zendesk.fixture"), "https://zendesk.fixture")
    if not (s.zendesk_base_url and s.zendesk_email and s.zendesk_api_token):
        raise ConnectorError("Zendesk is not configured: set ZENDESK_BASE_URL, ZENDESK_EMAIL, ZENDESK_API_TOKEN")
    return ZendeskConnector(_client(s.zendesk_base_url, (f"{s.zendesk_email}/token", s.zendesk_api_token)), s.zendesk_base_url)
