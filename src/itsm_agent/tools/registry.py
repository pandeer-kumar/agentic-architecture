"""Tool catalog from contracts/ (MCP tools + harness tools), synced into mcp_servers / mcp_tools."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache

from .. import db
from ..config import REPO_ROOT

# Tools with a working implementation in this build (Phase 1). Everything else stays in the
# catalog as 'disabled' until its connector is built; the router never offers disabled tools.
IMPLEMENTED = {
    "jira_issues_search_to_resultset", "jira_issues_count_approx", "jira_issue_get", "jira_fields_list", "jira_issue_comment_add",
    "servicenow_records_search_to_resultset", "servicenow_record_get", "servicenow_work_note_add",
    "zendesk_tickets_search_to_resultset", "zendesk_tickets_count", "zendesk_ticket_get", "zendesk_ticket_comment_add",
    "analyzer_resultset_describe", "analyzer_records_sample", "analyzer_resultset_filter", "analyzer_resultset_union",
    "analyzer_resultset_join", "analyzer_classify", "analyzer_aggregate", "analyzer_list_create", "analyzer_export",
    "teams_message_post", "teams_channels_list",
}


@lru_cache
def mcp_contracts() -> dict[str, dict]:
    out = {}
    for f in sorted((REPO_ROOT / "contracts/tools").glob("*.json")):
        c = json.loads(f.read_text())
        for t in c["tools"]:
            out[t["name"]] = {**t, "server": c["server"]}
    return out


@lru_cache
def harness_contracts() -> dict[str, dict]:
    h = json.loads((REPO_ROOT / "contracts/harness_tools.json").read_text())
    return {t["name"]: {**t, "server": "harness", "risk_tier": "read", "allowed_roles": t["roles"]} for t in h["tools"]}


def contract(name: str) -> dict | None:
    return mcp_contracts().get(name) or harness_contracts().get(name)


def schema_hash(t: dict) -> str:
    body = {k: t.get(k) for k in ("name", "description", "input_schema", "annotations")}
    return hashlib.sha256(db.dumps(body).encode()).hexdigest()


def api_tool(name: str) -> dict:
    """Anthropic tool definition (strict schema)."""
    from ..config import get_settings
    t = contract(name)
    d = {"name": name, "description": t["description"], "input_schema": t["input_schema"]}
    if get_settings().strict_tools:
        d["strict"] = True          # the gateway validates arguments either way
    return d


def sync_to_db(admin_user_id: str) -> None:
    """Register the built-in servers and tools; implemented tools enabled with an approved schema hash."""
    servers = {}
    for name, t in mcp_contracts().items():
        servers.setdefault(t["server"], []).append(t)
    with db.tx() as c:
        for server, tools in servers.items():
            row = c.execute("""INSERT INTO mcp_servers (name, source, transport, auth, status, approved_by, approved_at)
                               VALUES (%s,'builtin','in-process',%s,'enabled',%s,now())
                               ON CONFLICT (name) DO UPDATE SET updated_at = now() RETURNING id""",
                            (server, db.j({"type": "vault", "vault_path": f"kv/itsm/{server}"}), admin_user_id)).fetchone()
            for t in tools:
                h = schema_hash(t)
                enabled = t["name"] in IMPLEMENTED
                c.execute("""INSERT INTO mcp_tools (server_id, native_name, name, description, input_schema, annotations, schema_hash,
                                                    approved_schema_hash, risk_tier, allowed_roles, status, approved_by)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                             ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description, input_schema = EXCLUDED.input_schema,
                               schema_hash = EXCLUDED.schema_hash, last_seen_at = now()""",
                          (row["id"], t["name"], t["name"], t["description"], db.j(t["input_schema"]), db.j(t["annotations"]), h,
                           h if enabled else None, t["risk_tier"], t["allowed_roles"], "enabled" if enabled else "disabled",
                           admin_user_id if enabled else None))


def enabled_tools() -> list[dict]:
    rows = db.q("""SELECT name, allowed_roles, risk_tier FROM mcp_tools
                   WHERE status = 'enabled' AND approved_schema_hash = schema_hash""")
    return [{**contract(r["name"]), "allowed_roles": r["allowed_roles"]} for r in rows if contract(r["name"])]
