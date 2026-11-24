"""A real MCP server per tool namespace (jira, servicenow, zendesk, analyzer, teams).

Every call still goes through the tool gateway: schema validation, policy, write -> pending approval,
fencing and audit apply exactly as for the built-in agent. Each MCP connection gets its own session
and job, owned by the user named in ITSM_MCP_USER.

Run:  python -m itsm_agent.mcp_servers jira
"""
from __future__ import annotations

import json
import os
import sys

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .. import db, seed
from ..tools import gateway
from ..tools.impl import ToolContext
from ..tools.registry import IMPLEMENTED, mcp_contracts


def _context() -> ToolContext:
    from ..harness.orchestrator import append_event, new_session
    subject = os.environ.get("ITSM_MCP_USER", "mcp-client")
    uid = seed.ensure_user(subject, f"{subject}@localhost", subject)
    sid = new_session(uid, "MCP session")
    ev = append_event(sid, "user_message", {"text": "(MCP client session)"})
    job = db.q1("INSERT INTO jobs (session_id, user_id, request_event, status) VALUES (%s,%s,%s,'running') RETURNING id", (sid, uid, ev))
    return ToolContext(job_id=str(job["id"]), session_id=sid, user_id=uid, user_tz="UTC", user_turn_event_id=ev)


def build(namespace: str) -> Server:
    tools = {n: t for n, t in mcp_contracts().items() if t["server"] == namespace and n in IMPLEMENTED}
    if not tools:
        raise SystemExit(f"no implemented tools for namespace {namespace!r}")
    state: dict = {}

    async def list_tools(ctx, params):
        out = []
        for n, t in sorted(tools.items()):
            a = t["annotations"]
            out.append(types.Tool(name=n, description=t["description"], inputSchema=t["input_schema"],
                                  annotations=types.ToolAnnotations(read_only_hint=a.get("readOnlyHint"), destructive_hint=a.get("destructiveHint"),
                                                                    idempotent_hint=a.get("idempotentHint"), open_world_hint=a.get("openWorldHint"))))
        return types.ListToolsResult(tools=out)

    async def call_tool(ctx, params):
        if "tctx" not in state:
            state["tctx"] = await anyio.to_thread.run_sync(_context)
        name, args = params.name, params.arguments or {}
        if name not in tools:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"unknown tool {name}")], is_error=True)
        text, is_err = await anyio.to_thread.run_sync(lambda: gateway.call(state["tctx"], name, args))
        structured = None
        try:
            parsed = json.loads(text)
            structured = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], structured_content=structured, is_error=is_err)

    return Server(f"itsm-{namespace}", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool)


async def _serve(namespace: str) -> None:
    server = build(namespace)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m itsm_agent.mcp_servers <jira|servicenow|zendesk|analyzer|teams>")
    anyio.run(_serve, sys.argv[1])
