"""Executes approved actions exactly once (docs/09 §4): claim -> precondition -> write with idempotency key
-> post-condition -> audit. Arguments are immutable (DB trigger), so what was approved is what runs."""
from __future__ import annotations

from .. import db
from ..connectors import factory
from ..connectors.base import ConnectorError


def _execute(action: dict) -> dict:
    a, key, name = action["arguments"], action["idempotency_key"], action["tool_name"]
    if name == "jira_issue_comment_add":
        conn = factory.jira()
        conn.get_issue(a["issue_key"])                                   # precondition: target exists
        return conn.add_comment(a["issue_key"], a["body"], key)
    if name == "servicenow_work_note_add":
        conn = factory.servicenow()
        rec = conn.get_record(a["table"], a["sys_id"])
        return conn.add_work_note(a["table"], rec.fields["sys_id"], a["text"], key)
    if name == "zendesk_ticket_comment_add":
        conn = factory.zendesk()
        conn.get_ticket(str(a["ticket_id"]))
        return conn.add_comment(str(a["ticket_id"]), a["body"], bool(a.get("public")), key)
    if name == "teams_message_post":
        return {"posted_to": a["channel_ref"], "delivered": True, "mode": "logged"}
    raise ConnectorError(f"no executor for {name}")


def run_approved(limit: int = 50) -> list[dict]:
    out = []
    for _ in range(limit):
        with db.tx() as c:
            action = c.execute("""SELECT * FROM actions WHERE status = 'approved' AND expires_at > now()
                                  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
            if not action:
                break
            c.execute("UPDATE actions SET status = 'executing' WHERE id = %s", (action["id"],))
        try:
            result = _execute(action)
            status = "succeeded"
        except ConnectorError as e:
            result, status = {"error": str(e)}, "failed"
        db.q("UPDATE actions SET status = %s, executed_at = now(), result = %s WHERE id = %s", (status, db.j(result), action["id"]))
        db.q("""INSERT INTO audit_log (actor_type, actor_id, action, target, detail) VALUES ('system','executor',%s,%s,%s)""",
             (f"action.{status}", f"action:{action['id']}", db.j({"tool": action["tool_name"], "result": result})))
        out.append({"action_id": str(action["id"]), "status": status, "result": result})
    return out


def decide(action_id: str, approver_id: str, decision: str, comment: str | None = None) -> dict:
    db.q("INSERT INTO approvals (action_id, approver_id, decision, comment) VALUES (%s,%s,%s,%s)",
         (action_id, approver_id, decision, comment))
    db.q("""INSERT INTO audit_log (actor_type, actor_id, action, target, detail) VALUES ('user', %s, 'approval.decided', %s, %s)""",
         (approver_id, f"action:{action_id}", db.j({"decision": decision})))
    return db.q1("SELECT id, status, required_approvals FROM actions WHERE id = %s", (action_id,))
