"""Real MCP over stdio: list tools with the contract schemas, call one through the gateway."""
import json
import os
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from itsm_agent.config import get_settings


def test_mcp_stdio_roundtrip():
    s = get_settings()
    env = {**os.environ, "ITSM_DATABASE_URL": s.database_url, "ITSM_CONNECTOR_MODE": "fixture",
           "ITSM_FIXED_NOW": s.fixed_now, "ITSM_MCP_USER": "mcp-test"}

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "itsm_agent.mcp_servers", "servicenow"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                tools = await sess.list_tools()
                names = {t.name for t in tools.tools}
                assert "servicenow_record_get" in names and "servicenow_work_note_add" in names
                ok = await sess.call_tool("servicenow_record_get", {"table": "change_request", "number_or_sys_id": "CHG0030001",
                                                                    "response_format": "concise"})
                assert not ok.is_error and "untrusted_record" in ok.content[0].text
                bad = await sess.call_tool("servicenow_record_get", {"table": "nope"})
                assert bad.is_error
                write = await sess.call_tool("servicenow_work_note_add", {"table": "change_request", "sys_id": "x", "text": "hi"})
                assert json.loads(write.content[0].text)["status"] == "pending_approval"     # gateway policy applies over MCP too
    anyio.run(run)
