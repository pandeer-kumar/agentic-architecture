"""Implementations behind the tool contracts. Reads execute here; writes only ever become
proposals (the gateway intercepts them) and execute later in executor.py after approval."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Callable

from .. import db
from ..config import get_settings
from ..connectors import factory
from ..connectors.base import ConnectorError, FetchSummary, OverLimit, Window
from ..context import timewindow
from ..dataplane import analyzer, recordstore
from ..dataplane.map_phase import MapPhase
from ..memory import ltm, stm


class ToolError(Exception):
    """Raised for user-correctable problems; the message is returned to the model as is_error."""


@dataclass
class ToolContext:
    job_id: str
    session_id: str
    user_id: str
    user_tz: str
    agent_role: str = "orchestrator"
    user_turn_event_id: int | None = None
    epoch_id: str | None = None
    signals: dict = field(default_factory=dict)      # side channel to the harness (request_tools, submit_answer)


# --------------------------------------------------------------------------------- ledger helpers
def ledger_add(job_id: str, key: str, item: Any) -> None:
    db.q("""UPDATE jobs SET ledger = jsonb_set(ledger, %s, coalesce(ledger->%s, '[]'::jsonb) || %s::jsonb, true)
            WHERE id = %s""", ([key], key, db.dumps([item]), job_id))


# --------------------------------------------------------------------------------- harness tools
def plan_query_spec(ctx: ToolContext, a: dict) -> dict:
    try:
        fixed = get_settings().fixed_now
        from datetime import datetime
        now = datetime.fromisoformat(fixed) if fixed else None
        start, end = timewindow.resolve(a["relative_window"], ctx.user_tz, now)
    except timewindow.WindowError as e:
        raise ToolError(str(e))
    row = db.q1("""INSERT INTO query_specs (job_id, spec, window_start, window_end, window_field, user_tz)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (ctx.job_id, db.j(a), start, end, a["window_field"], ctx.user_tz))
    from zoneinfo import ZoneInfo
    z = ZoneInfo(ctx.user_tz)
    spec = {"query_spec_id": str(row["id"]), "window_utc": [start.isoformat(), end.isoformat()],
            "window_local": [start.astimezone(z).isoformat(), end.astimezone(z).isoformat()], "user_tz": ctx.user_tz,
            "window_field": a["window_field"], "systems": a["systems"], "rubric_id": a.get("rubric_id")}
    db.q("UPDATE jobs SET ledger = ledger || %s::jsonb WHERE id = %s",
         (db.dumps({"goal": a["intent"], "window_utc": spec["window_utc"], "user_tz": ctx.user_tz}), ctx.job_id))
    ledger_add(ctx.job_id, "query_specs", {"id": spec["query_spec_id"], "window_field": a["window_field"], "systems": a["systems"]})
    spec["next"] = ("Pass query_spec_id to each *_to_resultset tool. Do not put dates in JQL, encoded queries or "
                    "Zendesk queries: the connector adds this window.")
    return spec


def ledger_update(ctx: ToolContext, a: dict) -> dict:
    db.q("""UPDATE jobs SET ledger = jsonb_set(ledger, %s, %s::jsonb, true) WHERE id = %s""",
         (["steps", a["step_key"]], db.dumps({"status": a["status"], "notes": a["notes"][:300]}), ctx.job_id))
    return {"ok": True}


def scratchpad_write(ctx: ToolContext, a: dict) -> dict:
    try:
        stm.compress_scratchpad(ctx.job_id)
        return stm.scratchpad_write(ctx.job_id, a["entry_type"], a["text"][:300], a["refs"], a.get("supersedes_id"))
    except stm.ScratchpadRejected as e:
        raise ToolError(f"scratchpad rejected: {e}. Refs must be fact:F-n, rs:<handle>, rec:<system>:<id> or sp:<id> from this job.")


def request_tools(ctx: ToolContext, a: dict) -> dict:
    ctx.signals["request_tools"] = a["capability"]
    return {"status": "accepted", "note": "A new epoch will start with matching tools if any are enabled for you."}


def referent_resolve(ctx: ToolContext, a: dict) -> dict:
    try:
        return stm.resolve_referent(ctx.session_id, a["referent_id"])
    except KeyError as e:
        raise ToolError(str(e))


def memory_search(ctx: ToolContext, a: dict) -> dict:
    rows = ltm.search(a["query"], ctx.user_id, a.get("kinds") or None, max(1, min(int(a.get("limit") or 8), 20)))
    return {"items": [{"id": str(r["id"]), "kind": r["kind"], "subject": r["subject_key"], "content": r["content"],
                       "version": r["version"], "confidence": float(r["confidence"]), "as_of": r["as_of"].isoformat()} for r in rows]}


def memory_propose(ctx: ToolContext, a: dict) -> dict:
    try:
        return ltm.propose(ctx.job_id, ctx.session_id, ctx.user_id, a["kind"], a["subject_key"], a["content"], a["source_refs"], a["scope"])
    except ltm.MemoryRejected as e:
        raise ToolError(str(e))


def memory_flag_conflict(ctx: ToolContext, a: dict) -> dict:
    try:
        return ltm.flag_conflict(a["memory_id"], a["note"])
    except ltm.MemoryRejected as e:
        raise ToolError(str(e))


def submit_answer(ctx: ToolContext, a: dict) -> dict:
    ctx.signals["submit_answer"] = a
    return {"status": "received"}            # replaced by the verification verdict in the harness


# --------------------------------------------------------------------------------- bulk fetch
def _spec(ctx: ToolContext, spec_id: str) -> dict:
    s = db.q1("SELECT * FROM query_specs WHERE id = %s AND job_id = %s", (spec_id, ctx.job_id))
    if not s:
        raise ToolError("Unknown query_spec_id for this job. Call plan_query_spec first and pass its query_spec_id.")
    return s


def _bulk(ctx: ToolContext, system: str, spec_id: str, filter_query: str, fields: list[str], max_records: int | None,
          fetch_kw: dict | None = None) -> dict:
    spec = _spec(ctx, spec_id)
    limit = min(int(max_records or get_settings().max_records), get_settings().max_records)
    window = Window(spec["window_start"].astimezone(timezone.utc), spec["window_end"].astimezone(timezone.utc), spec["window_field"])
    conn = {"jira": factory.jira, "servicenow": factory.servicenow, "zendesk": factory.zendesk}[system]()
    rs = recordstore.create_fetch_set(ctx.job_id, spec_id, system, limit)
    summary = FetchSummary()
    try:
        for page in conn.fetch(filter_query, fields, window, limit, summary, **(fetch_kw or {})):
            recordstore.write_page(rs, page)
    except OverLimit as e:
        recordstore.mark_over_limit(rs, e)
        return {"status": "over_limit", "count": e.count, "limit": e.limit,
                "advice": "Narrow the filter or the window, or aggregate in an analytical source. Do not answer from a partial set."}
    except ConnectorError as e:
        db.q("UPDATE result_sets SET status = 'failed', completeness = %s WHERE id = %s", (db.j({"status": "incomplete", "error": str(e)}), rs["id"]))
        raise ToolError(f"{system} fetch failed: {e}")
    final = recordstore.finalize(rs, summary, window)
    ledger_add(ctx.job_id, "handles", {"handle": final["handle"], "source": system, "rows": final["unique_rows"],
                                       "completeness": final["completeness_status"]})
    return recordstore.handle_summary(final)


def jira_search(ctx, a):
    return _bulk(ctx, "jira", a["query_spec_id"], a["jql"], a["fields"], a.get("max_records"))


def servicenow_search(ctx, a):
    return _bulk(ctx, "servicenow", a["query_spec_id"], a["encoded_query"], a["fields"], a.get("max_records"), {"table": a["table"]})


def zendesk_search(ctx, a):
    return _bulk(ctx, "zendesk", a["query_spec_id"], a["query"], [], a.get("max_records"))


# --------------------------------------------------------------------------------- single-record reads
def _fenced(rec) -> dict:
    return {"source": f"{rec.source_system}:{rec.source_id}", "url": rec.source_url, "status": rec.status,
            "fields": rec.fields, "content": f'<untrusted_record source="{rec.source_system}:{rec.source_id}">'
                                             f"{rec.title}. {rec.body_text[:2000]}</untrusted_record>"}


def jira_get(ctx, a):
    try:
        return _fenced(factory.jira().get_issue(a["issue_key"]))
    except ConnectorError as e:
        raise ToolError(str(e))


def jira_count(ctx, a):
    try:
        return {"approximate_count": factory.jira().approximate_count(a["jql"]),
                "note": "Approximate. Never report it as the count; use a result set + analyzer_aggregate."}
    except ConnectorError as e:
        raise ToolError(str(e))


def jira_fields(ctx, a):
    s = get_settings()
    return {"fields": [{"id": "summary"}, {"id": "status"}, {"id": "components"}, {"id": "issuetype"}, {"id": "project"},
                       {"id": "resolutiondate"}, {"id": s.jira_cab_field, "name": "CAB approval"},
                       {"id": s.jira_linked_change_field, "name": "Linked ServiceNow change"}],
            "jql_names": ["project", "issuetype", "component", "status"]}


def servicenow_get(ctx, a):
    try:
        return _fenced(factory.servicenow().get_record(a["table"], a["number_or_sys_id"]))
    except ConnectorError as e:
        raise ToolError(str(e))


def zendesk_get(ctx, a):
    try:
        return _fenced(factory.zendesk().get_ticket(str(a["ticket_id"])))
    except ConnectorError as e:
        raise ToolError(str(e))


def zendesk_count(ctx, a):
    try:
        return {"count": factory.zendesk().count(a["query"])}
    except ConnectorError as e:
        raise ToolError(str(e))


def teams_channels(ctx, a):
    return {"channels": [{"channel_ref": c} for c in get_settings().risk_policy.get("teams_channels", ["noc-changes", "service-desk"])]}


# --------------------------------------------------------------------------------- analyzer
def _wrap(fn: Callable, *args, **kw):
    try:
        return fn(*args, **kw)
    except (analyzer.AnalyzerError, KeyError) as e:
        raise ToolError(str(e).strip("'"))


def an_describe(ctx, a): return _wrap(analyzer.describe, ctx.job_id, a["handle"])
def _track(ctx, res: dict, what: str) -> dict:
    ledger_add(ctx.job_id, "handles", {"handle": res["handle"], "source": what, "rows": res["row_count"],
                                       "completeness": (res.get("completeness") or {}).get("status")})
    return res


def an_filter(ctx, a):
    return _track(ctx, _wrap(analyzer.filter_, ctx.job_id, a["handle"], a["filters"], a.get("label_filter")), f"filter of {a['handle']}")


def an_union(ctx, a):
    return _track(ctx, _wrap(analyzer.union, ctx.job_id, a["handles"], a["dedupe_key"]), f"union of {','.join(a['handles'])}")


def an_join(ctx, a):
    res = _wrap(analyzer.join, ctx.job_id, a["left"], a["right"], a["left_key"], a["right_key"])
    for k in ("matched_left", "left_only", "right_only"):
        ledger_add(ctx.job_id, "handles", {"handle": res[k], "source": f"join {k}", "rows": res.get(k.replace("matched_left", "matched") + "_count")})
    return res


def an_sample(ctx, a):
    rows = _wrap(analyzer.sample, ctx.job_id, a["handle"], a.get("n") or 10, a.get("filters"), a.get("label_filter"))
    return {"records": [{"source": f"{r['source_system']}:{r['source_id']}", "status": r["status"], "fields": r["fields"],
                         "content": f'<untrusted_record source="{r["source_system"]}:{r["source_id"]}">{r["title"]}. {r["body_text"][:600]}</untrusted_record>'}
                        for r in rows], "note": "For inspection only. Never count from samples."}


def an_aggregate(ctx, a):
    res = _wrap(analyzer.aggregate, ctx.job_id, a["handle"], a["metric"], a.get("filters"), a.get("group_by"),
                a.get("metric_field"), a.get("label_filter"), a.get("description"))
    ledger_add(ctx.job_id, "facts", {"key": res["fact_key"], "what": res["description"], "value": res["value"]})
    return res


def an_list(ctx, a):
    res = _wrap(analyzer.list_create, ctx.job_id, a["handle"], a["columns"], a.get("filters"), a.get("label_filter"), a.get("sort"))
    ledger_add(ctx.job_id, "lists", {"key": res["list_key"], "rows": res["row_count"]})
    return res


def an_classify(ctx, a):
    try:
        return MapPhase(ctx.job_id, a["rubric_id"]).run(a["handle"], a.get("only_undecided", True))
    except (ValueError, KeyError) as e:
        raise ToolError(str(e))


def an_export(ctx, a):
    _wrap(analyzer.list_rows, ctx.job_id, a["list_key"], 1)
    return {"url": f"/v1/jobs/{ctx.job_id}/lists/{a['list_key']}.csv", "format": "csv"}


IMPL: dict[str, Callable[[ToolContext, dict], Any]] = {
    "plan_query_spec": plan_query_spec, "ledger_update": ledger_update, "scratchpad_write": scratchpad_write,
    "request_tools": request_tools, "referent_resolve": referent_resolve, "memory_search": memory_search,
    "memory_propose": memory_propose, "memory_flag_conflict": memory_flag_conflict, "submit_answer": submit_answer,
    "jira_issues_search_to_resultset": jira_search, "jira_issues_count_approx": jira_count, "jira_issue_get": jira_get,
    "jira_fields_list": jira_fields, "servicenow_records_search_to_resultset": servicenow_search,
    "servicenow_record_get": servicenow_get, "zendesk_tickets_search_to_resultset": zendesk_search,
    "zendesk_tickets_count": zendesk_count, "zendesk_ticket_get": zendesk_get, "teams_channels_list": teams_channels,
    "analyzer_resultset_describe": an_describe, "analyzer_records_sample": an_sample, "analyzer_resultset_filter": an_filter,
    "analyzer_resultset_union": an_union, "analyzer_resultset_join": an_join, "analyzer_classify": an_classify,
    "analyzer_aggregate": an_aggregate, "analyzer_list_create": an_list, "analyzer_export": an_export,
}
