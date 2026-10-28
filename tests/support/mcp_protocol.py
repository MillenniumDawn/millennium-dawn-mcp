"""Small helpers for assertions over MCP SDK call results."""

from __future__ import annotations

import json
from typing import Any


def tool_payload(result: Any) -> dict:
    """Decode the JSON dictionary returned by a tool, structured or text encoded."""
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    content = result.content if hasattr(result, "content") else result
    return json.loads(content[0].text)
