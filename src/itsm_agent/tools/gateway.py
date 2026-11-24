"""Tool gateway (docs/06 §3): one chain for every call.
validate -> policy/RBAC -> write => pending_approval -> rate limit -> execute -> size cap -> fence -> audit."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from jsonschema import Draft202012Validator

from .. import db
from ..config import get_settings
from .impl import IMPL, ToolContext, ToolError
from .registry import contract

WRITE_TIERS = {"medium", "high", "critical"}
_TIER_ORDER = ["read", "low", "medium", "high", "critical"]
_sem = {s: threading.BoundedSemaphore(4) for s in ("jira", "servicenow", "zendesk", "teams")}


def effective_tier(name: str, args: dict) -> str:
    t = contract(name)["risk_tier"]
    i = _TIER_ORDER.index(t)
    if name == "zendesk_ticket_comment_add" and args.get("public"):
        i = max(i, _TIER_ORDER.index("high"))
    return _TIER_ORDER[i]


def _preview(name: str, args: dict) -> dict:
    target = args.get("issue_key") or args.get("sys_id") or args.get("ticket_id") or args.get("channel_ref")
    text = args.get("body") or args.get("text") or args.get("text_md") or ""
    return {"tool": name, "target": target, "text": text[:1000], "args": args}


def call(ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
    """Returns (result_text_for_model, is_error). Never raises for tool-level problems."""
    t0 = time.monotonic()
    c = contract(name)
    decision, is_error, handle = "allow", False, None
    try:
        if c is None:
            raise ToolError(f"Unknown tool {name}. Use one of the tools you were given, or request_tools(capability).")
        if ctx.agent_role not in (c.get("allowed_roles") or c.get("roles") or []):
            decision = "deny"
            raise ToolError(f"{name} is not available to the {ctx.agent_role} role.")
        if c["server"] != "harness":
            status = db.q1("SELECT status FROM mcp_tools WHERE name = %s AND approved_schema_hash = schema_hash", (name,))
            if not status or status["status"] != "enabled":
                decision = "deny"
                raise ToolError(f"{name} is not enabled (pending review, suspended, or not implemented in this build).")
        errors = sorted(Draft202012Validator(c["input_schema"]).iter_errors(args), key=lambda e: list(e.path))
        if errors:
            raise ToolError("Invalid arguments: " + "; ".join(f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors[:5]))
        if "max_records" in args and args["max_records"] is not None and not (1 <= int(args["max_records"]) <= get_settings().max_records):
            raise ToolError(f"max_records must be between 1 and {get_settings().max_records}.")
        tier = effective_tier(name, args)
        if tier in WRITE_TIERS:
            decision = "pending_approval"
            result: Any = _propose(ctx, name, args, tier)
        else:
            sem = _sem.get(c["server"])
            if sem:
                with sem:
                    result = IMPL[name](ctx, args)
            else:
                result = IMPL[name](ctx, args)
        text = json.dumps(result, default=str)
        if isinstance(result, dict):
            handle = result.get("handle")
        cap = get_settings().max_inline_tokens * 4
        if len(text) > cap:
            text = text[:cap] + ' ... [truncated: result too large; narrow the query or use a *_to_resultset tool]'
    except ToolError as e:
        is_error, text = True, str(e)
    except Exception as e:                                          # noqa: BLE001 — surfaced to the model, logged
        is_error, text = True, f"Tool failed unexpectedly: {type(e).__name__}: {str(e)[:300]}"
    db.q("""INSERT INTO tool_calls (job_id, tool_name, arguments, policy_decision, is_error, result_handle, result_tokens, latency_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
         (ctx.job_id, name, db.j(args), decision, is_error, handle, len(text) // 4, int((time.monotonic() - t0) * 1000)))
    return text, is_error


def _propose(ctx: ToolContext, name: str, args: dict, tier: str) -> dict:
    if ctx.user_turn_event_id is None:
        raise ToolError("Writes must be requested by the user in this conversation.")
    policy = get_settings().risk_policy["tiers"][tier]
    key = f"{ctx.job_id}:{name}:{hashlib.sha256(db.dumps(args).encode()).hexdigest()[:16]}"
    existing = db.q1("SELECT id, status FROM actions WHERE idempotency_key = %s", (key,))
    if existing:
        return {"status": existing["status"], "approval_id": str(existing["id"]), "note": "already proposed"}
    row = db.q1("""INSERT INTO actions (job_id, user_turn_event_id, tool_name, arguments, risk_tier, required_approvals, preview,
                                        idempotency_key, expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now() + make_interval(hours => %s)) RETURNING id""",
                (ctx.job_id, ctx.user_turn_event_id, name, db.j(args), tier, policy["approvals"], db.j(_preview(name, args)),
                 key, policy.get("expiry_hours") or 24))
    db.q("""INSERT INTO audit_log (actor_type, actor_id, action, target, detail) VALUES ('agent', %s, 'action.proposed', %s, %s)""",
         (f"job:{ctx.job_id}", f"action:{row['id']}", db.j({"tool": name, "tier": tier})))
    return {"status": "pending_approval", "approval_id": str(row["id"]), "risk_tier": tier,
            "required_approvals": policy["approvals"],
            "note": "Nothing has been changed yet. A human must approve this action; tell the user it is awaiting approval."}
