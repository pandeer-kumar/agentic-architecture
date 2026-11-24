"""Zendesk connector: export search (cursor, no 1,000 cap) + search count for reconciliation."""
from __future__ import annotations

from datetime import timedelta
from typing import Iterator

import httpx

from .base import (CanonicalRecord, ConnectorError, FetchSummary, OverLimit, Page, Window, parse_dt,
                   request_with_retry, sanitize)

WINDOW_FIELD = {"created": "created", "updated": "updated", "resolved": "solved", "closed": "solved"}
RECORD_FIELD = {"created": "created_at", "updated": "updated_at", "solved": "updated_at"}


class ZendeskConnector:
    system = "zendesk"

    def __init__(self, client: httpx.Client, base_url: str):
        self.client, self.base = client, base_url.rstrip("/")

    def windowed_query(self, query: str, window: Window) -> str:
        f = WINDOW_FIELD[window.field]
        iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
        # '>' is exclusive, so start one second earlier; out-of-window rows are re-filtered in UTC (docs/04 §3 C5)
        q = query.strip()
        if "type:" not in q:
            q = f"type:ticket {q}".strip()
        return f"{q} {f}>{iso(window.start - timedelta(seconds=1))} {f}<{iso(window.end)}"

    def count(self, query: str) -> int:
        r = request_with_retry(self.client, "GET", f"{self.base}/api/v2/search/count", params={"query": query})
        return int(r.json()["count"])

    def fetch(self, filter_query: str, fields: list[str], window: Window, max_records: int,
              summary: FetchSummary) -> Iterator[Page]:
        query = self.windowed_query(filter_query, window)
        summary.source_reported_total = self.count(query)
        if summary.source_reported_total > max_records:
            raise OverLimit(summary.source_reported_total, max_records)
        after, seen, page_no = None, set(), 0
        while True:
            page_no += 1
            if page_no > max_records // 100 + 3:
                raise ConnectorError("page cap exceeded (pagination loop guard)")
            params = {"query": query, "filter[type]": "ticket", "page[size]": 100}
            if after:
                params["page[after]"] = after
            data = request_with_retry(self.client, "GET", f"{self.base}/api/v2/search/export", params=params).json()
            meta = data.get("meta", {})
            has_more, nxt = meta.get("has_more", False), meta.get("after_cursor")
            if has_more and nxt in seen:
                raise ConnectorError("repeated cursor (pagination loop guard)")
            seen.add(nxt)
            rows = data.get("results", [])
            if not rows and has_more:
                raise ConnectorError("empty page while more pages announced (pagination loop guard)")
            records = [self._normalise(t, window) for t in rows]
            summary.pages += 1
            summary.fetched_rows += len(records)
            yield Page(page_no, after, nxt if has_more else None, records)
            if not has_more:
                summary.exhausted = True
                return
            after = nxt

    def _normalise(self, t: dict, window: Window) -> CanonicalRecord:
        tid = str(t["id"])
        return CanonicalRecord(
            source_system="zendesk", source_id=tid, source_url=f"{self.base}/agent/tickets/{tid}",
            title=sanitize(t.get("subject"), 500), body_text=sanitize(t.get("description")), status=t.get("status"),
            created_at=parse_dt(t.get("created_at")), updated_at=parse_dt(t.get("updated_at")),
            event_at=parse_dt(t.get(RECORD_FIELD[WINDOW_FIELD[window.field]])),
            fields={"priority": t.get("priority"), "tags": t.get("tags", []), "category": "unknown",
                    "cab_status": "unknown", "change_ref": f"ZD-{tid}", "requester_id": t.get("requester_id")})

    def get_ticket(self, ticket_id: str) -> CanonicalRecord:
        r = request_with_retry(self.client, "GET", f"{self.base}/api/v2/tickets/{ticket_id}.json")
        return self._normalise(r.json()["ticket"], Window(None, None, "updated"))

    def add_comment(self, ticket_id: str, body: str, public: bool, idempotency_key: str) -> dict:
        request_with_retry(self.client, "PUT", f"{self.base}/api/v2/tickets/{ticket_id}.json",
                           json={"ticket": {"comment": {"body": body, "public": public}}},
                           headers={"Idempotency-Key": idempotency_key})
        return {"ticket_id": ticket_id}
