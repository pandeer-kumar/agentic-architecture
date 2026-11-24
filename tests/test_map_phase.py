"""Map phase: coverage is always 100 %; injected model faults are caught; labels match ground truth."""
from itsm_agent import db
from itsm_agent.tools.impl import IMPL


def _union(ctx):
    spec = IMPL["plan_query_spec"](ctx, {"intent": "network changes", "relative_window": "last 24 hours", "window_field": "closed",
                                         "systems": ["servicenow", "jira"], "rubric_id": "network_change_v3"})
    sn = IMPL["servicenow_records_search_to_resultset"](ctx, {"query_spec_id": spec["query_spec_id"], "table": "change_request",
                                                               "encoded_query": "", "fields": []})
    jr = IMPL["jira_issues_search_to_resultset"](ctx, {"query_spec_id": spec["query_spec_id"],
                                                        "jql": "project in (NET, OPS) AND issuetype = Change", "fields": []})
    return IMPL["analyzer_resultset_union"](ctx, {"handles": [sn["handle"], jr["handle"]], "dedupe_key": "change_ref"})


def _network_count(ctx, handle):
    return IMPL["analyzer_aggregate"](ctx, {"handle": handle, "metric": "count", "label_filter": "network_change_v3:network_change",
                                            "description": "network"})["value"]["value"]


def test_clean_run_matches_truth(ctx, brain, vendors):
    u = _union(ctx)
    res = IMPL["analyzer_classify"](ctx, {"handle": u["handle"], "rubric_id": "network_change_v3", "only_undecided": True})
    assert res["coverage"]["missing"] == 0 and res["coverage"]["labelled"] == u["row_count"]
    assert res["deterministic"] > 0 and res["judged_by_model"] > 0       # pre-filter took part of the load
    assert _network_count(ctx, u["handle"]) == vendors.ds.truth["network_changes"]


def test_faults_are_caught_and_coverage_holds(ctx, brain, vendors):
    brain.faults = {"drop": 0.05, "dup": 0.03, "fake_quote": 0.05, "truncate": 0.05, "invent": 0.2, "flip": 0.02}
    u = _union(ctx)
    res = IMPL["analyzer_classify"](ctx, {"handle": u["handle"], "rubric_id": "network_change_v3", "only_undecided": True})
    assert res["coverage"]["missing"] == 0                                  # every record has a final label
    hits = res["validator_hits"]
    assert hits.get("V1_missing", 0) > 0 and hits.get("V2_ungrounded", 0) > 0
    # no surviving ungrounded positives: every final model-made positive quotes its own record
    bad = db.q("""SELECT a.evidence_quote, r.title, r.body_text FROM record_annotations a JOIN records r ON r.id = a.record_id
                  WHERE a.job_id = %s AND a.is_final AND a.label = 'network_change' AND a.source <> 'deterministic_rule'""", (ctx.job_id,))
    for b in bad:
        assert " ".join(b["evidence_quote"].lower().split()) in " ".join((b["title"] + ". " + b["body_text"]).lower().split())
    truth = vendors.ds.truth["network_changes"]
    got = _network_count(ctx, u["handle"])
    assert abs(got - truth) <= max(2, truth // 100) + res["coverage"]["needs_human_review"]
