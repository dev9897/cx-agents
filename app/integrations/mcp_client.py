"""MCP client — discovers tools from the MCP server via SSE."""

import asyncio
import json
import logging
import os
from typing import Any, Optional

from langchain_core.tools import StructuredTool
from mcp import ClientSession
from mcp.client.sse import sse_client
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger("sap_agent.mcp")

MCP_HOST = os.getenv("MCP_HOST", "localhost")
MCP_PORT = os.getenv("MCP_PORT", "8005")
MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}/sse"

_JSON_TYPE_MAP = {
    "string": str, "integer": int, "number": float,
    "boolean": bool, "array": list, "object": dict,
}


def _build_args_schema(name: str, input_schema: dict) -> type[BaseModel]:
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))
    field_definitions = {}
    for field_name, field_info in properties.items():
        py_type = _JSON_TYPE_MAP.get(field_info.get("type", "string"), str)
        description = field_info.get("description", "")
        default = field_info.get("default")
        if field_name in required:
            field_definitions[field_name] = (py_type, Field(..., description=description))
        else:
            field_definitions[field_name] = (Optional[py_type], Field(default=default, description=description))
    return create_model(f"{name}_Schema", **field_definitions)


async def _call_tool_async(name: str, kwargs: dict) -> Any:
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


MCP_CONNECT_TIMEOUT = float(os.getenv("MCP_CONNECT_TIMEOUT", "5"))


async def _fetch_tools_async() -> list[Any]:
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
                        func=sync_fn, name=t_name,
                        description=t_desc or t_name, args_schema=t_args_model,
                    )
                tools.append(make_sync_tool(mcp_tool.name, mcp_tool.description or "", args_model))
    return tools


def get_tools_sync() -> list[Any]:
    """Sync entry point — called at agent startup."""
    try:
        tools = asyncio.run(asyncio.wait_for(
            _fetch_tools_async(), timeout=MCP_CONNECT_TIMEOUT,
        ))
        logger.info("Loaded %d tools from MCP server at %s", len(tools), MCP_URL)
        return tools
    except Exception as e:
        logger.warning("MCP connection failed (%s) — falling back to direct SAP tools", e)
        from app.agent.tools import get_direct_sap_tools
        return get_direct_sap_tools()


def call_mcp_tool_sync(name: str, kwargs: dict) -> dict:
    """Call an MCP tool synchronously. Used to bridge UI auth with MCP vault."""
    try:
        return asyncio.run(_call_tool_async(name, kwargs))
    except Exception as e:
        logger.warning("call_mcp_tool_sync(%s) failed: %s", name, e)
        return {"success": False, "error": str(e)}


def get_mcp_session_id() -> Optional[str]:
    try:
        result = asyncio.run(asyncio.wait_for(
            _call_tool_async("get_static_session", {}),
            timeout=MCP_CONNECT_TIMEOUT,
        ))
        if result.get("success"):
            return result["session_id"]
    except Exception as e:
        logger.warning("Could not fetch MCP session_id: %s", e)
    return None
