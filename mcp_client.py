"""
mcp_client.py — uses native MCP SDK directly, no langchain-mcp-adapters
"""
import asyncio
import os
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model
import json

MCP_HOST = os.getenv("MCP_HOST", "localhost")
MCP_PORT  = os.getenv("MCP_PORT", "8005")
MCP_URL   = f"http://{MCP_HOST}:{MCP_PORT}/sse"

# JSON Schema type → Python type mapping
_JSON_TYPE_MAP = {
    "string":  str,
    "integer": int,
    "number":  float,
    "boolean": bool,
    "array":   list,
    "object":  dict,
}


def _build_args_schema(name: str, input_schema: dict) -> type[BaseModel]:
    """Convert an MCP tool's JSON Schema inputSchema into a Pydantic model."""
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))
    field_definitions = {}

    for field_name, field_info in properties.items():
        json_type = field_info.get("type", "string")
        py_type = _JSON_TYPE_MAP.get(json_type, str)
        description = field_info.get("description", "")
        default = field_info.get("default")

        if field_name in required:
            field_definitions[field_name] = (
                py_type,
                Field(..., description=description),
            )
        else:
            # Optional field with a default
            field_definitions[field_name] = (
                Optional[py_type],
                Field(default=default, description=description),
            )

    model = create_model(f"{name}_Schema", **field_definitions)
    return model


async def _call_tool_async(name: str, kwargs: dict) -> Any:
    """Open a fresh SSE connection, call a single tool, return the result."""
    async with sse_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await session.call_tool(name, arguments=kwargs)
            if r.content:
                raw = r.content[0].text if hasattr(r.content[0], "text") else str(r.content[0])
                try:
                    return json.loads(raw)
                except Exception:
                    return {"result": raw}
            return {"success": False, "error": "No response"}


async def _fetch_tools_async() -> list[Any]:
    """Connect to MCP server, discover tools, wrap as LangChain StructuredTools."""
    tools = []

    async with sse_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()

            for mcp_tool in result.tools:
                schema = mcp_tool.inputSchema or {"type": "object", "properties": {}}
                args_model = _build_args_schema(mcp_tool.name, schema)

                def make_sync_tool(t_name, t_desc, t_args_model):
                    def sync_fn(**kwargs):
                        return asyncio.run(_call_tool_async(t_name, kwargs))
                    sync_fn.__name__ = t_name
                    return StructuredTool.from_function(
                        func=sync_fn,
                        name=t_name,
                        description=t_desc or t_name,
                        args_schema=t_args_model,
                    )

                tools.append(make_sync_tool(
                    mcp_tool.name,
                    mcp_tool.description or "",
                    args_model,
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


def get_mcp_session_id() -> Optional[str]:
    """Fetch the static session_id from the MCP server (if configured)."""
    try:
        result = asyncio.run(_call_tool_async("get_static_session", {}))
        if result.get("success"):
            session_id = result["session_id"]
            print(f"✅ MCP static session_id: {session_id}")
            return session_id
    except Exception as e:
        print(f"⚠️  Could not fetch MCP session_id: {e}")
    return None
