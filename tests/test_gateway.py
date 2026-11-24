"""Tool gateway: validation, policy, write -> approval, idempotent proposals."""
import json

from itsm_agent import db
from itsm_agent.tools import gateway


def test_invalid_arguments_are_actionable_errors(ctx):
    text, err = gateway.call(ctx, "analyzer_aggregate", {"handle": "rs_X"})
    assert err and "Invalid arguments" in text and "metric" in text


def test_unknown_and_disabled_tools(ctx):
    assert gateway.call(ctx, "made_up_tool", {})[1]
    text, err = gateway.call(ctx, "genie_space_ask", {"space_id": "s", "question": "q", "conversation_id": "c"})
    assert err and "not enabled" in text


def test_role_restrictions(ctx):
    ctx.agent_role = "explorer"
    text, err = gateway.call(ctx, "jira_issue_comment_add", {"issue_key": "NET-1", "body": "x", "visibility_group": ""})
    assert err and "not available" in text


def test_write_becomes_pending_approval_and_is_idempotent(ctx, vendors):
    before = len(vendors.writes)
    args = {"issue_key": "NET-1000", "body": "CAB approval missing", "visibility_group": ""}
    t1, e1 = gateway.call(ctx, "jira_issue_comment_add", args)
    t2, e2 = gateway.call(ctx, "jira_issue_comment_add", args)
    r1, r2 = json.loads(t1), json.loads(t2)
    assert not e1 and r1["status"] == "pending_approval" and r1["approval_id"] == r2["approval_id"]
    assert len(vendors.writes) == before                       # nothing written upstream
    assert db.q1("SELECT count(*) AS n FROM actions WHERE job_id = %s", (ctx.job_id,))["n"] == 1


def test_max_records_bound_enforced_in_code(ctx):
    text, err = gateway.call(ctx, "zendesk_tickets_search_to_resultset", {"query_spec_id": "x", "query": "vpn", "max_records": 5000})
    assert err and "max_records" in text
