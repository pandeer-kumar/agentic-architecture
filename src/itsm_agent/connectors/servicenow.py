"""ServiceNow connector: Table API with offset paging ordered by sys_id, X-Total-Count reconciliation."""
from __future__ import annotations

from typing import Iterator
from zoneinfo import ZoneInfo

import httpx

from .base import (CanonicalRecord, ConnectorError, FetchSummary, OverLimit, Page, Window, parse_dt,
                   request_with_retry, sanitize)

WINDOW_FIELD = {"created": "sys_created_on", "updated": "sys_updated_on", "resolved": "closed_at", "closed": "closed_at"}
BASE_FIELDS = ["sys_id", "number", "short_description", "description", "state", "category", "approval",
               "assignment_group", "cmdb_ci", "sys_created_on", "sys_updated_on", "closed_at"]
PAGE = 500


def _val(v):
    """Table API returns either plain values or {display_value, value, link} objects."""
    if isinstance(v, dict):
        return v.get("display_value") or v.get("value")
    return v


class ServiceNowConnector:
    system = "servicenow"

    def __init__(self, client: httpx.Client, base_url: str, tz: str = "UTC"):
        self.client, self.base, self.tz = client, base_url.rstrip("/"), ZoneInfo(tz)

    def windowed_query(self, encoded: str, window: Window) -> str:
        if "ORDERBY" in encoded.upper():
            raise ConnectorError("Remove ORDERBY from the encoded query; the connector orders by sys_id.")
        f = WINDOW_FIELD[window.field]
        fmt = lambda d: d.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S")
        parts = [p for p in [encoded.strip("^ "), f"{f}>={fmt(window.start)}", f"{f}<{fmt(window.end)}", "ORDERBYsys_id"] if p]
        return "^".join(parts)

    def fetch(self, filter_query: str, fields: list[str], window: Window, max_records: int,
              summary: FetchSummary, table: str = "change_request") -> Iterator[Page]:
        query = self.windowed_query(filter_query, window)
        want = ",".join(sorted(set(BASE_FIELDS + list(fields))))
        offset, page_no = 0, 0
        while True:
            page_no += 1
            if page_no > max_records // PAGE + 3:
                raise ConnectorError("page cap exceeded (pagination loop guard)")
            r = request_with_retry(self.client, "GET", f"{self.base}/api/now/table/{table}",
                                   params={"sysparm_query": query, "sysparm_fields": want, "sysparm_limit": PAGE,
                                           "sysparm_offset": offset, "sysparm_exclude_reference_link": "true"})
            total = int(r.headers.get("X-Total-Count", "-1"))
            if total >= 0:
                summary.source_reported_total = total
                if total > max_records:
                    raise OverLimit(total, max_records)
            rows = r.json().get("result", [])
            records = [self._normalise(x, table, window) for x in rows]
            offset += len(rows)
            last = len(rows) < PAGE or (total >= 0 and offset >= total)
            if not rows and not last:
                raise ConnectorError("empty page while more pages announced (pagination loop guard)")
            summary.pages += 1
            summary.fetched_rows += len(records)
            yield Page(page_no, str(offset - len(rows)), None if last else str(offset), records)
            if last:
                summary.exhausted = True
                return

    def _normalise(self, x: dict, table: str, window: Window) -> CanonicalRecord:
        number = _val(x.get("number"))
        approval = (_val(x.get("approval")) or "").lower()
        return CanonicalRecord(
            source_system="servicenow", source_id=number,
            source_url=f"{self.base}/nav_to.do?uri={table}.do?sys_id={_val(x.get('sys_id'))}",
            title=sanitize(_val(x.get("short_description")), 500), body_text=sanitize(_val(x.get("description"))),
            status=_val(x.get("state")), created_at=parse_dt(_val(x.get("sys_created_on"))),
            updated_at=parse_dt(_val(x.get("sys_updated_on"))), event_at=parse_dt(_val(x.get(WINDOW_FIELD[window.field]))),
            fields={"table": table, "sys_id": _val(x.get("sys_id")), "category": (_val(x.get("category")) or "unknown").lower(),
                    "approval": approval or None,
                    "cab_status": {"approved": "approved", "rejected": "rejected"}.get(approval, "not_approved" if approval else "unknown"),
                    "assignment_group": _val(x.get("assignment_group")), "cmdb_ci": _val(x.get("cmdb_ci")),
                    "change_ref": number})

    def get_record(self, table: str, number_or_sys_id: str) -> CanonicalRecord:
        field = "sys_id" if len(number_or_sys_id) == 32 else "number"
        r = request_with_retry(self.client, "GET", f"{self.base}/api/now/table/{table}",
                               params={"sysparm_query": f"{field}={number_or_sys_id}", "sysparm_limit": 1,
                                       "sysparm_fields": ",".join(BASE_FIELDS)})
        rows = r.json().get("result", [])
        if not rows:
            raise ConnectorError(f"{table} {number_or_sys_id} not found")
        return self._normalise(rows[0], table, Window(None, None, "closed"))

    def add_work_note(self, table: str, sys_id: str, text: str, idempotency_key: str) -> dict:
        r = request_with_retry(self.client, "PATCH", f"{self.base}/api/now/table/{table}/{sys_id}",
                               json={"work_notes": text}, headers={"X-Idempotency-Key": idempotency_key})
        return {"sys_id": sys_id, "updated": bool(r.json().get("result"))}
