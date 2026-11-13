"""Minimal MCP server exposing structured output for serialization protocol tests."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from md_mcp.util.response import enforce_budget

server = MCPServer("mcp-response-budget-test")


@server.tool(structured_output=True)
def measured_payload(item_count: int = 90) -> dict[str, Any]:
    """Return a synthetic near-limit payload through structured and text channels."""
    items = [
        {"name": f"focus_{index}", "description": "café\\line\n" + "x" * 620}
        for index in range(item_count)
    ]
    return enforce_budget(
        {
            "ok": True,
            "items": items,
            "total": len(items),
        },
        heavy_keys=("items",),
    )


if __name__ == "__main__":
    server.run()
