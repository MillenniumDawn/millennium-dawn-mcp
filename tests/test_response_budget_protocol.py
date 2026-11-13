"""Regression coverage for the serialized MCP tool-result output budget."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent
from pydantic_core import to_json

from md_mcp.util.response import BUDGET_BYTES, enforce_budget
from tests.support.mcp_protocol import mcp_result_size, tool_payload
from tests.support.stdio import server_parameters


def test_enforce_budget_caps_actual_mcp_result_with_nested_unicode_and_duplicates() -> None:
    items = [
        {"name": f"focus_{index}", "description": "café\\line\n" + "x" * 620}
        for index in range(145)
    ]
    payload = {
        "ok": True,
        "items": items,
        "total": len(items),
    }

    # This fixture fits the old compact one-copy estimate but not the bytes emitted by MCP.
    compact_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert compact_size <= BUDGET_BYTES
    text_size = len(to_json(payload, fallback=str, indent=2))
    assert text_size > BUDGET_BYTES
    assert mcp_result_size(payload) > BUDGET_BYTES

    bounded = enforce_budget(payload, heavy_keys=("items",))

    assert bounded.get("size_truncated") is True
    assert bounded.get("items_dropped") == len(items)
    assert bounded["total"] == len(items)
    assert mcp_result_size(bounded) <= BUDGET_BYTES


@pytest.mark.integration
def test_focus_graph_stdio_result_uses_sdk_serialization_budget(real_mod_root, tmp_path) -> None:
    async def call_tool():
        async with (
            stdio_client(server_parameters(real_mod_root, tmp_path / "cache")) as (read, write),
            ClientSession(read, write, read_timeout_seconds=600.0) as session,
        ):
            await session.initialize()
            return await session.call_tool("focus_graph", {"tag": "ISR", "detail": "summary"})

    result = asyncio.run(call_tool())
    payload = tool_payload(result)

    assert payload["ok"] is True
    assert payload["tag"] == "ISR"
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == to_json(payload, fallback=str, indent=2).decode("utf-8")
    assert len(result.content[0].text.encode("utf-8")) <= BUDGET_BYTES
    assert result.structured_content is None
    actual = result.model_dump_json(by_alias=True, exclude_unset=True).encode("utf-8")
    assert len(actual) <= BUDGET_BYTES


@pytest.mark.parametrize("item_count", [90, 145])
def test_structured_stdio_result_matches_sdk_budget_estimator(item_count: int) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tests.support.structured_mcp_server"],
    )

    async def call_tool():
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write, read_timeout_seconds=30.0) as session,
        ):
            await session.initialize()
            return await session.call_tool("measured_payload", {"item_count": item_count})

    result = asyncio.run(call_tool())
    payload = tool_payload(result)

    assert isinstance(result.content[0], TextContent)
    assert result.structured_content == payload
    assert payload["size_truncated"] is True
    assert payload["items_dropped"] == item_count
    assert payload["total"] == item_count
    assert result.content[0].text == to_json(payload, fallback=str, indent=2).decode("utf-8")
    assert len(result.content[0].text.encode("utf-8")) <= BUDGET_BYTES
    actual = result.model_dump_json(by_alias=True, exclude_unset=True).encode("utf-8")
    assert mcp_result_size(payload) == len(actual)
    assert len(actual) <= BUDGET_BYTES
