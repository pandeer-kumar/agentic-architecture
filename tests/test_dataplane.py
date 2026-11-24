"""Record store + analyzer: completeness proof, union dedupe, exact facts, reproducibility, lists."""
from itsm_agent import db
from itsm_agent.dataplane import analyzer
from itsm_agent.tools.impl import IMPL


def _plan(ctx):
    return IMPL["plan_query_spec"](ctx, {"intent": "changes", "relative_window": "last 24 hours", "window_field": "closed",
                                         "systems": ["servicenow", "jira"], "rubric_id": None})


def test_fetch_union_and_exact_counts(ctx, vendors):
    spec = _plan(ctx)
    sn = IMPL["servicenow_records_search_to_resultset"](ctx, {"query_spec_id": spec["query_spec_id"], "table": "change_request",
                                                               "encoded_query": "", "fields": ["number"]})
    jr = IMPL["jira_issues_search_to_resultset"](ctx, {"query_spec_id": spec["query_spec_id"],
                                                        "jql": "project in (NET, OPS) AND issuetype = Change", "fields": []})
    assert sn["completeness"]["status"] == "proven" and jr["completeness"]["status"] == "proven"
    u = IMPL["analyzer_resultset_union"](ctx, {"handles": [sn["handle"], jr["handle"]], "dedupe_key": "change_ref"})
    assert u["row_count"] == vendors.ds.truth["changes_in_window"]           # 513: linked duplicates removed
    assert u["duplicates_removed"] == sn["row_count"] + jr["row_count"] - u["row_count"]
    f = IMPL["analyzer_aggregate"](ctx, {"handle": u["handle"], "metric": "count", "description": "all changes",
                                         "group_by": ["source_system"]})
    assert f["value"]["total"] == 513
    fact = db.q1("SELECT * FROM facts WHERE job_id = %s AND fact_key = %s", (ctx.job_id, f["fact_key"]))
    assert analyzer.recompute_fact(fact) == fact["value"]                    # D5 reproducibility


def test_list_rows_are_rendered_from_db(ctx):
    spec = _plan(ctx)
    sn = IMPL["servicenow_records_search_to_resultset"](ctx, {"query_spec_id": spec["query_spec_id"], "table": "change_request",
                                                               "encoded_query": "category=network", "fields": ["number"]})
    lst = IMPL["analyzer_list_create"](ctx, {"handle": sn["handle"], "columns": ["source_id", "cab_status"],
                                             "filters": [{"field": "cab_status", "op": "neq", "value": "approved"}]})
    cols, rows, total = analyzer.list_rows(ctx.job_id, lst["list_key"])
    assert total == len(rows) == lst["row_count"]
    assert all(r["cab_status"] != "approved" for r in rows)


def test_handles_are_scoped_to_their_job(ctx, session, user):
    from itsm_agent.tools.impl import ToolContext
    spec = _plan(ctx)
    sn = IMPL["servicenow_records_search_to_resultset"](ctx, {"query_spec_id": spec["query_spec_id"], "table": "change_request",
                                                               "encoded_query": "", "fields": []})
    other = db.q1("INSERT INTO jobs (session_id, user_id, request_event) VALUES (%s,%s,%s) RETURNING id",
                  (session, user, ctx.user_turn_event_id))
    import pytest
    from itsm_agent.tools.impl import ToolError
    with pytest.raises(ToolError):
        IMPL["analyzer_resultset_describe"](ToolContext(str(other["id"]), session, user, "UTC"), {"handle": sn["handle"]})


def test_lossy_source_is_not_reported_as_proven(ctx):
    from datetime import timezone
    from itsm_agent.connectors import factory
    from itsm_agent.connectors.base import FetchSummary, Window
    from itsm_agent.dataplane import recordstore
    fv = factory.fake_vendors()
    fv.lossy_pages = True
    try:
        spec = _plan(ctx)
        s = db.q1("SELECT * FROM query_specs WHERE id = %s", (spec["query_spec_id"],))
        w = Window(s["window_start"].astimezone(timezone.utc), s["window_end"].astimezone(timezone.utc), "closed")
        rs = recordstore.create_fetch_set(ctx.job_id, spec["query_spec_id"], "jira", 2000)
        summ = FetchSummary()
        for page in factory.jira().fetch("project in (NET, OPS) AND issuetype = Change", [], w, 2000, summ):
            recordstore.write_page(rs, page)
        final = recordstore.finalize(rs, summ, w)
        assert final["completeness_status"] in ("bounded", "incomplete")
        assert final["completeness"]["approximate_count"] > final["unique_rows"]
    finally:
        fv.lossy_pages = False
