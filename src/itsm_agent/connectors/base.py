"""Shared connector types: canonical records, pages, completeness proof (docs/04 §2-4)."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol

import httpx


@dataclass
class Window:
    start: datetime          # inclusive, UTC
    end: datetime            # exclusive, UTC
    field: str               # created | updated | resolved | closed


@dataclass
class CanonicalRecord:
    source_system: str
    source_id: str
    source_url: str
    title: str
    body_text: str
    status: str | None
    created_at: datetime | None
    updated_at: datetime | None
    event_at: datetime | None          # value of the window field
    fields: dict[str, Any]

    @property
    def content_hash(self) -> str:
        payload = json.dumps({"id": self.source_id, "title": self.title, "body": self.body_text,
                              "status": self.status, "fields": self.fields}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class Page:
    page_no: int
    cursor_in: str | None
    cursor_out: str | None             # None on the last page
    records: list[CanonicalRecord]


@dataclass
class FetchSummary:
    pages: int = 0
    fetched_rows: int = 0
    source_reported_total: int | None = None
    approximate_count: int | None = None
    exhausted: bool = False
    notes: list[str] = field(default_factory=list)


class ConnectorError(RuntimeError):
    pass


class OverLimit(ConnectorError):
    def __init__(self, count: int, limit: int):
        super().__init__(f"{count} records match; limit is {limit}. Narrow the query or use an aggregate.")
        self.count, self.limit = count, limit


class BulkConnector(Protocol):
    system: str
    def fetch(self, filter_query: str, fields: list[str], window: Window, max_records: int,
              summary: FetchSummary) -> Iterator[Page]: ...


SANITIZE_MAX = 8000
_STRIP = {ord(c): None for c in "​‌‍⁠﻿‪‫‬‭‮⁦⁧⁨⁩"}


def sanitize(text: str | None, limit: int = SANITIZE_MAX) -> str:
    """Strip control, zero-width and bidi-override characters; collapse whitespace; cap length (docs/05 §5)."""
    if not text:
        return ""
    text = text.translate(_STRIP)
    text = "".join(ch if (ch >= " " or ch in "\n\t") else " " for ch in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + f" [truncated: {len(text) - limit} chars]"


def parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            d = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def request_with_retry(client: httpx.Client, method: str, url: str, *, attempts: int = 5, **kw) -> httpx.Response:
    """Retry 429/5xx with backoff, honouring Retry-After (docs/06 §3 stage 6)."""
    delay = 0.5
    for i in range(attempts):
        try:
            r = client.request(method, url, **kw)
        except httpx.TransportError as e:
            if i == attempts - 1:
                raise ConnectorError(f"transport error: {e}") from e
            time.sleep(delay); delay *= 2
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if i == attempts - 1:
                raise ConnectorError(f"{r.status_code} from {url} after {attempts} attempts")
            ra = r.headers.get("Retry-After")
            time.sleep(min(float(ra), 30.0) if ra and ra.replace('.', '', 1).isdigit() else delay)
            delay *= 2
            continue
        if r.status_code >= 400:
            raise ConnectorError(f"{r.status_code} from {url}: {r.text[:300]}")
        return r
    raise ConnectorError("unreachable")
