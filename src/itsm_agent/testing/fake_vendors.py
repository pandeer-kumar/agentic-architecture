"""Fake Jira / ServiceNow / Zendesk APIs (same request/response shapes as the real ones) over a
seeded synthetic dataset with known ground truth. Used in 'fixture' connector mode and in tests.

Mount with httpx.MockTransport(FakeVendors(...).handle) so the real connector code runs unchanged.
"""
from __future__ import annotations

import base64
import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx

NET_TEXT = ["Updated firewall rule on edge {d} for partner VPN", "Replaced core switch {d} in rack B4",
            "Added VLAN 214 on access switch {d}", "BGP session reconfigured on router {d}",
            "Load balancer pool change on {d}", "DNS forwarder change on {d}", "Subnet 10.42.0.0/24 re-addressed on {d}"]
NON_TEXT = ["Patched application server {d}", "Database index rebuild on {d}", "Rotated TLS certificate for portal {d}",
            "Desktop image update for floor {d}", "Upgraded HR app to v{d}", "Network printer driver update on floor {d}"]
DEVICES = ["SW-DEL-02", "RTR-BLR-01", "FW-EDGE-03", "LB-PRD-07", "SW-MUM-11", "RTR-HYD-04", "DNS-CORE-2"]


@dataclass
class Dataset:
    now: datetime
    sn_changes: list[dict] = field(default_factory=list)
    jira_issues: list[dict] = field(default_factory=list)
    zendesk_tickets: list[dict] = field(default_factory=list)
    truth: dict = field(default_factory=dict)


def generate(now: datetime, n_sn: int = 400, n_jira: int = 700, n_zendesk: int = 150, seed: int = 7,
             window_hours: int = 24, link_rate: float = 0.15) -> Dataset:
    rnd = random.Random(seed)
    ds = Dataset(now=now)
    start = now - timedelta(hours=window_hours)

    def when():
        return now - timedelta(seconds=rnd.randint(60, 2 * window_hours * 3600))   # half inside the window

    for i in range(n_sn):
        is_net = rnd.random() < 0.45
        cat = rnd.choice(["network", "network", "other"]) if is_net else rnd.choice(["application", "database", "other", "hardware"])
        text = rnd.choice(NET_TEXT if is_net else NON_TEXT).format(d=rnd.choice(DEVICES) if is_net else rnd.randint(2, 40))
        approval = rnd.choices(["approved", "not requested", "rejected", "requested"], [86, 8, 3, 3])[0]
        closed = when()
        ds.sn_changes.append({"sys_id": f"{i:032x}", "number": f"CHG{30000 + i:07d}", "short_description": text,
                              "description": f"{text}. Implemented by NOC.", "state": "Closed", "category": cat,
                              "approval": approval, "assignment_group": "NOC-L2" if is_net else "AppOps",
                              "cmdb_ci": rnd.choice(DEVICES) if is_net else "APP-SRV",
                              "sys_created_on": (closed - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
                              "sys_updated_on": closed.strftime("%Y-%m-%d %H:%M:%S"),
                              "closed_at": closed.strftime("%Y-%m-%d %H:%M:%S"), "_net": is_net})
    for i in range(n_jira):
        linked = rnd.random() < link_rate and ds.sn_changes
        if linked:
            sn = rnd.choice(ds.sn_changes)
            is_net, text, res = sn["_net"], sn["short_description"], datetime.strptime(sn["closed_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        else:
            sn, is_net = None, rnd.random() < 0.4
            text = rnd.choice(NET_TEXT if is_net else NON_TEXT).format(d=rnd.choice(DEVICES) if is_net else rnd.randint(2, 40))
            res = when()
        project = "NET" if (is_net and rnd.random() < 0.8) else rnd.choice(["NET", "OPS"])
        comps = [{"name": "Network"}] if is_net and rnd.random() < 0.5 else ([{"name": "Platform"}] if rnd.random() < 0.3 else [])
        cab = rnd.choices(["Approved", "Not required", "Rejected", None], [84, 10, 2, 4])[0]
        ds.jira_issues.append({"id": str(10000 + i), "key": f"{project}-{1000 + i}", "fields": {
            "summary": text, "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text + ". Change executed per runbook."}]}]},
            "status": {"name": "Done"}, "issuetype": {"name": "Change"}, "project": {"key": project},
            "components": comps, "created": (res - timedelta(days=1)).isoformat(), "updated": res.isoformat(),
            "resolutiondate": res.isoformat(), "customfield_10050": {"value": cab} if cab else None,
            "customfield_10051": sn["number"] if sn else None, "assignee": {"displayName": "NOC Engineer"}},
            "_net": is_net})
    for i in range(n_zendesk):
        vpn = rnd.random() < 0.3
        upd = when()
        ds.zendesk_tickets.append({"id": 500 + i, "subject": "VPN keeps disconnecting" if vpn else "Password reset",
                                   "description": "Customer reports VPN drops every 10 minutes" if vpn else "Cannot log in",
                                   "status": "open", "priority": "high" if vpn else "normal", "tags": ["vpn"] if vpn else ["account"],
                                   "created_at": (upd - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "updated_at": upd.strftime("%Y-%m-%dT%H:%M:%SZ"), "requester_id": 900 + i})
    ds.truth = compute_truth(ds, start, now)
    return ds


def _utc(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def compute_truth(ds: Dataset, start: datetime, end: datetime) -> dict:
    """Ground truth for 'network changes closed in [start,end) and how many lack CAB approval'.
    Population: all ServiceNow change_requests + Jira issues (projects NET, OPS, issuetype Change);
    a Jira issue linked to a ServiceNow change is the same change (ServiceNow record wins)."""
    changes: dict[str, dict] = {}
    for c in ds.sn_changes:
        if start <= _utc(c["closed_at"]) < end:
            changes[c["number"]] = {"net": c["_net"], "approved": c["approval"] == "approved"}
    for i in ds.jira_issues:
        f = i["fields"]
        if not (start <= _utc(f["resolutiondate"]) < end):
            continue
        ref = f["customfield_10051"] or i["key"]
        if ref in changes:
            continue
        cab = (f["customfield_10050"] or {}).get("value") if f["customfield_10050"] else None
        changes[ref] = {"net": i["_net"], "approved": cab == "Approved"}
    net = [k for k, v in changes.items() if v["net"]]
    return {"changes_in_window": len(changes), "network_changes": len(net),
            "network_without_cab": sum(1 for k in net if not changes[k]["approved"]),
            "network_refs": sorted(net)}


# ------------------------------------------------------------------ fake HTTP APIs
class FakeVendors:
    """One handler for all three vendors, keyed by host. Fault injection for paging tests."""

    def __init__(self, ds: Dataset, jira_tz: str = "UTC", sn_tz: str = "UTC", duplicate_across_pages: bool = False,
                 rate_limit_first: bool = False, lossy_pages: bool = False):
        self.ds, self.jira_tz, self.sn_tz = ds, ZoneInfo(jira_tz), ZoneInfo(sn_tz)
        self.duplicate_across_pages, self.rate_limit_first = duplicate_across_pages, rate_limit_first
        self.lossy_pages = lossy_pages          # simulates records shifting between pages (one record never returned)
        self.calls: list[str] = []
        self.writes: dict[str, dict] = {}       # idempotency key -> write (exactly-once evidence)
        self._limited = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.host}{request.url.path}")
        if self.rate_limit_first and not self._limited:
            self._limited = True
            return httpx.Response(429, headers={"Retry-After": "0"})
        host = request.url.host
        if host.startswith("jira"):
            return self._jira(request)
        if host.startswith("servicenow"):
            return self._sn(request)
        if host.startswith("zendesk"):
            return self._zd(request)
        return httpx.Response(404)

    # ---- Jira
    def _jira_match(self, jql: str):
        jql = re.split(r"\s+ORDER BY\s+", jql, flags=re.I)[0].replace("(", " ").replace(")", " ")
        clauses = [c.strip() for c in re.split(r"\s+AND\s+", jql, flags=re.I) if c.strip()]
        def ok(issue):
            f = issue["fields"]
            for c in clauses:
                m = re.match(r'(\w+)\s*(>=|<=|!=|=|<|>|\bin\b)\s*(.+)', c, re.I)
                if not m:
                    raise ValueError(f"unsupported JQL clause: {c}")
                k, op, v = m.group(1).lower(), m.group(2).lower(), m.group(3).strip().strip('"')
                if k in ("created", "updated", "resolutiondate", "resolved"):
                    fv = _utc(f["resolutiondate" if k == "resolved" else k])
                    dv = datetime.strptime(v, "%Y-%m-%d %H:%M").replace(tzinfo=self.jira_tz)
                    if not {">=": fv >= dv, "<": fv < dv, ">": fv > dv, "<=": fv <= dv}[op]:
                        return False
                    continue
                actual = {"project": f["project"]["key"], "issuetype": f["issuetype"]["name"],
                          "component": [x["name"] for x in f["components"]], "status": f["status"]["name"]}.get(k)
                if actual is None:
                    raise ValueError(f"unknown field {k}")
                vals = [x.strip().strip('"') for x in v.split(",")] if op == "in" else [v]
                hit = (any(a in vals for a in actual) if isinstance(actual, list) else actual in vals)
                if (op in ("=", "in") and not hit) or (op == "!=" and hit):
                    return False
            return True
        return sorted([i for i in self.ds.jira_issues if ok(i)], key=lambda i: (i["key"].split("-")[0], int(i["key"].split("-")[1])))

    def _jira(self, r: httpx.Request) -> httpx.Response:
        if r.url.path == "/rest/api/3/search/approximate-count":
            try:
                return httpx.Response(200, json={"count": len(self._jira_match(json.loads(r.content)["jql"]))})
            except ValueError as e:
                return httpx.Response(400, json={"errorMessages": [str(e)]})
        if r.url.path == "/rest/api/3/search/jql":
            body = json.loads(r.content)
            try:
                hits = self._jira_match(body["jql"])
            except ValueError as e:
                return httpx.Response(400, json={"errorMessages": [str(e)]})
            off = int(base64.b64decode(body["nextPageToken"]).decode()) if body.get("nextPageToken") else 0
            size = min(int(body.get("maxResults", 50)), 100)
            page = hits[off:off + size]
            if self.duplicate_across_pages and off > 0:
                page = [hits[off - 1]] + page                               # overlap: previous page's last item again
            if self.lossy_pages and off > 0 and page:
                page = page[1:]                                             # shift: one record is never returned
            nxt = off + size
            out = {"issues": [{"id": i["id"], "key": i["key"], "fields": i["fields"]} for i in page], "isLast": nxt >= len(hits)}
            if nxt < len(hits):
                out["nextPageToken"] = base64.b64encode(str(nxt).encode()).decode()
            return httpx.Response(200, json=out)
        g = re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)", r.url.path)
        if g and r.method == "GET":
            hit = next((i for i in self.ds.jira_issues if i["key"] == g.group(1)), None)
            return httpx.Response(200, json={"id": hit["id"], "key": hit["key"], "fields": hit["fields"]}) if hit else httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
        m = re.match(r"/rest/api/3/issue/([A-Z]+-\d+)/comment", r.url.path)
        if m and r.method == "POST":
            return self._write(r, {"system": "jira", "issue": m.group(1), "body": json.loads(r.content)}, {"id": "c-" + m.group(1)})
        return httpx.Response(404)

    # ---- ServiceNow
    def _sn(self, r: httpx.Request) -> httpx.Response:
        m = re.match(r"/api/now/table/(\w+)(?:/(\w+))?", r.url.path)
        if not m:
            return httpx.Response(404)
        if r.method == "PATCH":
            return self._write(r, {"system": "servicenow", "sys_id": m.group(2), "body": json.loads(r.content)}, {"result": {"sys_id": m.group(2)}})
        qs = parse_qs(urlparse(str(r.url)).query)
        query = qs.get("sysparm_query", [""])[0]
        limit, offset = int(qs.get("sysparm_limit", ["100"])[0]), int(qs.get("sysparm_offset", ["0"])[0])
        rows = self.ds.sn_changes
        for cond in [c for c in query.split("^") if c]:
            if cond.startswith("ORDERBY"):
                key = cond[7:]
                rows = sorted(rows, key=lambda x: x[key])
                continue
            mm = re.match(r"(\w+)(>=|<=|!=|=|<|>|IN|LIKE)(.*)", cond)
            if not mm:
                return httpx.Response(400, json={"error": {"message": f"bad condition {cond}"}})
            k, op, v = mm.groups()
            if k in ("closed_at", "sys_created_on", "sys_updated_on"):
                dv = datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=self.sn_tz)
                cmp = {">=": lambda a: a >= dv, "<": lambda a: a < dv, ">": lambda a: a > dv, "<=": lambda a: a <= dv}[op]
                rows = [x for x in rows if cmp(datetime.strptime(x[k], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))]
            elif op == "=":
                rows = [x for x in rows if str(x.get(k)) == v]
            elif op == "!=":
                rows = [x for x in rows if str(x.get(k)) != v]
            elif op == "IN":
                vs = v.split(","); rows = [x for x in rows if str(x.get(k)) in vs]
            elif op == "LIKE":
                rows = [x for x in rows if v.lower() in str(x.get(k, "")).lower()]
        page = [{k: v for k, v in x.items() if not k.startswith("_")} for x in rows[offset:offset + limit]]
        return httpx.Response(200, json={"result": page}, headers={"X-Total-Count": str(len(rows))})

    # ---- Zendesk
    def _zd_match(self, query: str):
        rows = self.ds.zendesk_tickets
        for tok in query.split():
            m = re.match(r"(\w+)([:<>])(.+)", tok)
            if not m:
                rows = [t for t in rows if tok.lower() in (t["subject"] + " " + t["description"]).lower()]
                continue
            k, op, v = m.groups()
            if k == "type":
                continue
            if k == "tags":
                rows = [t for t in rows if v in t["tags"]]
            elif k in ("created", "updated", "solved"):
                field_ = {"created": "created_at", "updated": "updated_at", "solved": "updated_at"}[k]
                dv = _utc(v)
                rows = [t for t in rows if (_utc(t[field_]) > dv if op == ">" else _utc(t[field_]) < dv)]
            elif k in ("status", "priority"):
                rows = [t for t in rows if t[k] == v]
        return sorted(rows, key=lambda t: t["created_at"])

    def _zd(self, r: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(r.url)).query)
        if r.url.path == "/api/v2/search/count":
            return httpx.Response(200, json={"count": len(self._zd_match(qs["query"][0]))})
        if r.url.path == "/api/v2/search/export":
            hits = self._zd_match(qs["query"][0])
            size = int(qs.get("page[size]", ["100"])[0])
            off = int(base64.b64decode(qs["page[after]"][0]).decode()) if "page[after]" in qs else 0
            nxt = off + size
            return httpx.Response(200, json={"results": hits[off:nxt], "meta": {
                "has_more": nxt < len(hits), "after_cursor": base64.b64encode(str(nxt).encode()).decode() if nxt < len(hits) else None}})
        m = re.match(r"/api/v2/tickets/(\d+)\.json", r.url.path)
        if m and r.method == "GET":
            hit = next((t for t in self.ds.zendesk_tickets if str(t["id"]) == m.group(1)), None)
            return httpx.Response(200, json={"ticket": hit}) if hit else httpx.Response(404)
        if m and r.method == "PUT":
            return self._write(r, {"system": "zendesk", "ticket": m.group(1), "body": json.loads(r.content)}, {"ticket": {"id": int(m.group(1))}})
        return httpx.Response(404)

    def _write(self, r: httpx.Request, record: dict, reply: dict) -> httpx.Response:
        key = r.headers.get("X-Idempotency-Key") or r.headers.get("Idempotency-Key")
        if key and key in self.writes:
            return httpx.Response(200, json=reply)        # replay: no second write
        self.writes[key or f"nokey-{len(self.writes)}"] = record
        return httpx.Response(201 if r.method == "POST" else 200, json=reply)
