"""Short-term and long-term memory rules through the tools the model uses."""
import pytest

from itsm_agent import db
from itsm_agent.memory import ltm
from itsm_agent.tools.impl import IMPL, ToolError


def test_scratchpad_rejects_refs_from_outside_the_job(ctx):
    with pytest.raises(ToolError):
        IMPL["scratchpad_write"](ctx, {"entry_type": "finding", "text": "x", "refs": ["fact:F-99"], "supersedes_id": None})
    ok = IMPL["scratchpad_write"](ctx, {"entry_type": "hypothesis", "text": "maybe", "refs": [], "supersedes_id": None})
    assert ok["entry_type"] == "hypothesis"


def test_org_fact_from_tickets_only_waits_for_review(ctx):
    spec = IMPL["plan_query_spec"](ctx, {"intent": "x", "relative_window": "last 24 hours", "window_field": "closed",
                                         "systems": ["servicenow"], "rubric_id": None})
    IMPL["servicenow_records_search_to_resultset"](ctx, {"query_spec_id": spec["query_spec_id"], "table": "change_request",
                                                          "encoded_query": "", "fields": []})
    rec = db.q1("SELECT source_id FROM records r JOIN result_sets s ON s.id = r.result_set_id WHERE s.job_id = %s LIMIT 1", (ctx.job_id,))
    res = IMPL["memory_propose"](ctx, {"kind": "semantic", "subject_key": "noc.owner", "content": "NOC-L2 owns firewalls",
                                       "source_refs": [f"rec:servicenow:{rec['source_id']}"], "scope": "org"})
    assert res["status"] == "pending_review"
    assert not any(m["subject_key"] == "noc.owner" for m in ltm.search("NOC-L2 owns firewalls", ctx.user_id))
    with pytest.raises(ToolError):
        IMPL["memory_propose"](ctx, {"kind": "episodic", "subject_key": "x", "content": "y", "source_refs": ["rec:jira:NOPE-1"], "scope": "org"})


def test_pinned_org_profile_is_retrievable(ctx):
    assert "CAB approval" in ltm.pinned_profile()
