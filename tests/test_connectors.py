"""Real connector code against the fake vendor APIs: pagination, loop guards, retries, completeness inputs."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from itsm_agent.connectors.base import ConnectorError, FetchSummary, OverLimit, Window, sanitize
from itsm_agent.connectors.jira import JiraConnector
from itsm_agent.connectors.servicenow import ServiceNowConnector
from itsm_agent.connectors.zendesk import ZendeskConnector
from itsm_agent.testing.fake_vendors import FakeVendors, generate

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
WIN = Window(NOW - timedelta(hours=24), NOW, "closed")


def client(fv):
    return httpx.Client(transport=httpx.MockTransport(fv.handle))


def jira_truth(ds):
    return sum(1 for i in ds.jira_issues if i["fields"]["project"]["key"] in ("NET", "OPS")
               and WIN.start <= datetime.fromisoformat(i["fields"]["resolutiondate"]) < WIN.end)


def test_jira_paginates_to_exhaustion_and_dedupes_overlap():
    ds = generate(NOW, seed=3)
    fv = FakeVendors(ds, duplicate_across_pages=True)
    c = JiraConnector(client(fv), "https://jira.fixture")
    s = FetchSummary()
    seen = {}
    for page in c.fetch("project in (NET, OPS) AND issuetype = Change", [], WIN, 2000, s):
        for r in page.records:
            seen[r.source_id] = r
    assert s.exhausted and s.pages > 1
    assert s.fetched_rows > len(seen)                  # overlap really happened
    assert len(seen) == jira_truth(ds) == s.approximate_count


def test_jira_rejects_order_by_and_adds_window_in_account_timezone():
    c = JiraConnector(client(FakeVendors(generate(NOW))), "https://jira.fixture", tz="Asia/Kolkata")
    jql = c.windowed_jql("project = NET", WIN)
    assert 'resolutiondate >= "2026-11-22 14:30"' in jql and jql.endswith("ORDER BY key ASC")
    with pytest.raises(ConnectorError):
        c.windowed_jql("project = NET ORDER BY created", WIN)


def test_retry_on_429_honours_retry_after():
    fv = FakeVendors(generate(NOW), rate_limit_first=True)
    c = ServiceNowConnector(client(fv), "https://servicenow.fixture")
    s = FetchSummary()
    rows = [r for p in c.fetch("", [], WIN, 2000, s) for r in p.records]
    assert fv.calls[0] == fv.calls[1]                  # first call was retried
    assert len(rows) == s.source_reported_total


def test_over_limit_stops_before_fetching():
    c = ServiceNowConnector(client(FakeVendors(generate(NOW, n_sn=400))), "https://servicenow.fixture")
    with pytest.raises(OverLimit):
        list(c.fetch("", [], WIN, 50, FetchSummary()))


def test_repeated_page_token_is_a_hard_error():
    class Loop(FakeVendors):
        def _jira(self, r):
            if r.url.path.endswith("/search/jql"):
                return httpx.Response(200, json={"issues": [self.ds.jira_issues[0]], "nextPageToken": "same", "isLast": False})
            return super()._jira(r)
    c = JiraConnector(client(Loop(generate(NOW))), "https://jira.fixture")
    with pytest.raises(ConnectorError, match="loop guard"):
        list(c.fetch("project = NET", [], WIN, 2000, FetchSummary()))


def test_zendesk_export_matches_count():
    ds = generate(NOW, n_zendesk=350)
    c = ZendeskConnector(client(FakeVendors(ds)), "https://zendesk.fixture")
    s = FetchSummary()
    w = Window(WIN.start, WIN.end, "updated")
    rows = [r for p in c.fetch("tags:vpn", [], w, 2000, s) for r in p.records]
    assert s.exhausted and len(rows) == s.source_reported_total > 0


def test_sanitize_strips_bidi_and_zero_width():
    assert sanitize("ok‮evil​ text") == "okevil text"
