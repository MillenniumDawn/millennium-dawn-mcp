"""Small helpers for assertions over MCP SDK call results."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic_core import to_json


def mcp_result_size(payload: Any) -> int:
    """Measure the SDK's emitted CallToolResult, including text and structured copies."""
    text = to_json(payload, fallback=str, indent=2).decode("utf-8")
    result = CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=False,
    )
    return len(result.model_dump_json(by_alias=True, exclude_unset=True).encode("utf-8"))


def tool_payload(result: Any) -> dict:
    """Decode the JSON dictionary returned by a tool, structured or text encoded."""
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    content = result.content if hasattr(result, "content") else result
    return json.loads(content[0].text)
