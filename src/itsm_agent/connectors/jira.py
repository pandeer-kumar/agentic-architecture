"""Jira Cloud connector: enhanced JQL search (nextPageToken), approximate count, comments.

The model supplies a JQL *filter* without dates; this connector adds the frozen window
(on the spec's window field, in the Jira account's time zone) and stable ordering.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

import httpx

from .base import (CanonicalRecord, ConnectorError, FetchSummary, OverLimit, Page, Window, parse_dt,
                   request_with_retry, sanitize)

WINDOW_FIELD = {"created": "created", "updated": "updated", "resolved": "resolutiondate", "closed": "resolutiondate"}
BASE_FIELDS = ["summary", "description", "status", "created", "updated", "resolutiondate", "components",
               "issuetype", "project", "assignee"]


def _adf_text(node) -> str:
    """Flatten Atlassian Document Format (or plain string) to text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return (node.get("text") or "") + " ".join(_adf_text(c) for c in node.get("content", []) or [])
    if isinstance(node, list):
        return " ".join(_adf_text(c) for c in node)
    return str(node)


class JiraConnector:
    system = "jira"

    def __init__(self, client: httpx.Client, base_url: str, tz: str = "UTC",
                 cab_field: str = "customfield_10050", linked_change_field: str = "customfield_10051"):
        self.client, self.base = client, base_url.rstrip("/")
        self.tz, self.cab_field, self.linked_field = ZoneInfo(tz), cab_field, linked_change_field

    def windowed_jql(self, jql: str, window: Window) -> str:
        if " order by " in jql.lower():
            raise ConnectorError("Remove ORDER BY from the JQL; the connector adds stable ordering.")
        f = WINDOW_FIELD[window.field]
        fmt = lambda d: d.astimezone(self.tz).strftime("%Y-%m-%d %H:%M")
        return f'({jql}) AND {f} >= "{fmt(window.start)}" AND {f} < "{fmt(window.end)}" ORDER BY key ASC'

    def approximate_count(self, jql: str) -> int:
        r = request_with_retry(self.client, "POST", f"{self.base}/rest/api/3/search/approximate-count", json={"jql": jql})
        return int(r.json()["count"])

    def fetch(self, filter_query: str, fields: list[str], window: Window, max_records: int,
              summary: FetchSummary) -> Iterator[Page]:
        jql = self.windowed_jql(filter_query, window)
        summary.approximate_count = self.approximate_count(jql.split(" ORDER BY ")[0])
        if summary.approximate_count > max_records:
            raise OverLimit(summary.approximate_count, max_records)
        want = sorted(set(BASE_FIELDS + list(fields) + [self.cab_field, self.linked_field]))
        token, seen_tokens, page_no = None, set(), 0
        max_pages = max_records // 100 + 3
        while True:
            page_no += 1
            if page_no > max_pages:
                raise ConnectorError("page cap exceeded (pagination loop guard)")
            body = {"jql": jql, "fields": want, "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            data = request_with_retry(self.client, "POST", f"{self.base}/rest/api/3/search/jql", json=body).json()
            issues = data.get("issues", [])
            nxt = data.get("nextPageToken")
            last = data.get("isLast", nxt is None)
            if nxt and nxt in seen_tokens:
                raise ConnectorError("repeated nextPageToken (pagination loop guard)")
            if not issues and not last:
                raise ConnectorError("empty page while more pages announced (pagination loop guard)")
            seen_tokens.add(nxt)
            records = [self._normalise(i, window) for i in issues]
            summary.pages += 1
            summary.fetched_rows += len(records)
            yield Page(page_no, token, None if last else nxt, records)
            if last or not nxt:
                summary.exhausted = True
                return
            token = nxt

    def _normalise(self, issue: dict, window: Window) -> CanonicalRecord:
        f = issue.get("fields", {})
        cab = f.get(self.cab_field)
        cab_val = (cab.get("value") if isinstance(cab, dict) else cab) or None
        comps = [c.get("name") for c in (f.get("components") or []) if isinstance(c, dict)]
        status = (f.get("status") or {}).get("name") if isinstance(f.get("status"), dict) else f.get("status")
        linked = f.get(self.linked_field)
        event_raw = f.get(WINDOW_FIELD[window.field])
        return CanonicalRecord(
            source_system="jira", source_id=issue["key"], source_url=f"{self.base}/browse/{issue['key']}",
            title=sanitize(f.get("summary"), 500), body_text=sanitize(_adf_text(f.get("description"))),
            status=status, created_at=parse_dt(f.get("created")), updated_at=parse_dt(f.get("updated")),
            event_at=parse_dt(event_raw),
            fields={"project": (f.get("project") or {}).get("key") if isinstance(f.get("project"), dict) else f.get("project"),
                    "issuetype": (f.get("issuetype") or {}).get("name") if isinstance(f.get("issuetype"), dict) else f.get("issuetype"),
                    "components": comps, "category": "network" if any(c.lower() == "network" for c in comps) else "unknown",
                    "cab_status": {"approved": "approved", "rejected": "rejected"}.get(str(cab_val).lower(), "not_approved" if cab_val else "unknown"),
                    "cab_raw": cab_val, "change_ref": linked or issue["key"], "linked_change": linked,
                    "assignee": ((f.get("assignee") or {}).get("displayName") if isinstance(f.get("assignee"), dict) else f.get("assignee"))})

    def get_issue(self, key: str) -> CanonicalRecord:
        want = ",".join(sorted(set(BASE_FIELDS + [self.cab_field, self.linked_field])))
        r = request_with_retry(self.client, "GET", f"{self.base}/rest/api/3/issue/{key}", params={"fields": want})
        return self._normalise(r.json(), Window(datetime.min, datetime.max, "resolved"))

    def add_comment(self, issue_key: str, body: str, idempotency_key: str) -> dict:
        doc = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}]}
        r = request_with_retry(self.client, "POST", f"{self.base}/rest/api/3/issue/{issue_key}/comment",
                               json={"body": doc}, headers={"X-Idempotency-Key": idempotency_key})
        return {"comment_id": r.json().get("id"), "issue_key": issue_key}
