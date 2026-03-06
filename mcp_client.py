"""
mcp_client.py — uses native MCP SDK directly, no langchain-mcp-adapters
"""
import asyncio
import os
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from langchain_core.tools import StructuredTool
import json

MCP_HOST = os.getenv("MCP_HOST", "localhost")
MCP_PORT  = os.getenv("MCP_PORT", "8005")
MCP_URL   = f"http://{MCP_HOST}:{MCP_PORT}/sse"


async def _fetch_tools_async() -> list[Any]:
    """Connect to MCP server, discover tools, wrap as LangChain StructuredTools."""
    tools = []

    async with sse_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()

            for mcp_tool in result.tools:
                # Capture tool name in closure
                tool_name = mcp_tool.name

                async def _call(session=session, name=tool_name, **kwargs):
                    r = await session.call_tool(name, arguments=kwargs)
                    # Extract text content from MCP response
                    if r.content:
                        raw = r.content[0].text if hasattr(r.content[0], "text") else str(r.content[0])
                        try:
                            return json.loads(raw)
                        except Exception:
                            return {"result": raw}
                    return {"success": False, "error": "No response"}

                # Build a sync wrapper so LangGraph ToolNode can call it
                def make_sync_tool(async_fn, t_name, t_desc, t_schema):
                    def sync_fn(**kwargs):
                        return asyncio.run(async_fn(**kwargs))
                    sync_fn.__name__ = t_name
                    return StructuredTool.from_function(
                        func=sync_fn,
                        name=t_name,
                        description=t_desc or t_name,
                        args_schema=None,
                    )

                tools.append(make_sync_tool(
                    _call,
                    mcp_tool.name,
                    mcp_tool.description or "",
                    mcp_tool.inputSchema,
                ))

    return tools


def get_tools_sync() -> list[Any]:
    """Sync entry point — called at module load in production_agent.py"""
    try:
        tools = asyncio.run(_fetch_tools_async())
        print(f"✅ Loaded {len(tools)} tools from MCP server at {MCP_URL}")
        return tools
    except Exception as e:
        print(f"❌ MCP connection failed: {e}")
        print("   Falling back to direct SAP tools...")
        from sap_commerce_tools import ALL_TOOLS
        return ALL_TOOLS