"""MCP protocol round-trip tests.

Spins up the server with FastMCP's in-process API and exercises every tool +
resource. Catches serialisation regressions that pure-Python unit tests miss.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from md_mcp.config import Settings
from md_mcp.server import build_server


def _settings(mod_root, cache_dir) -> Settings:
    return Settings(
        mod_root=mod_root,
        vanilla_path=None,
        cache_dir=cache_dir,
        validator_mode="in_process",
        default_lang="en",
    )


def _text(result) -> str:
    """Pull the JSON text out of FastMCP's `call_tool` response."""
    return result[0].text


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)  # for pytest, no anyio dep


@pytest.fixture
def server(fake_mod_root, cache_dir):
    return build_server(_settings(fake_mod_root, cache_dir))


EXPECTED_TOOLS = {
    # M1
    "resolve_focus",
    "resolve_loc",
    "parse_file",
    "parse_string",
    "find_focuses",
    # M2 resolvers
    "resolve_sprite",
    "resolve_event",
    "resolve_decision",
    "resolve_idea",
    # M2 analysis
    "find_references",
    "list_country_content",
    # M2 validation
    "check_equipment_variant",
    "validate",
    "validate_list",
    "lint",
    "review_branch",
    "fix_lint",
    # M3 generators
    "generate_focus",
    "generate_event",
    "generate_decision",
    "generate_idea",
    "generate_gfx_entry",
    "generate_gfx_merge",
    "generate_loc_stub",
    # M3 analysis
    "focus_graph",
    "check_refs",
    "focus_layout",
    "diff_summary",
    "check_encoding",
}


def test_list_tools(server):
    async def go():
        return await server.list_tools()

    tools = asyncio.new_event_loop().run_until_complete(go())
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS


def test_call_resolve_focus(server):
    async def go():
        return await server.call_tool("resolve_focus", {"focus_id": "TST_root"})

    result = asyncio.new_event_loop().run_until_complete(go())
    payload = json.loads(_text(result))
    assert payload["ok"] is True
    assert payload["id"] == "TST_root"
    assert payload["file"].endswith("test.txt")


def test_call_resolve_loc(server):
    async def go():
        return await server.call_tool("resolve_loc", {"key": "TST_root"})

    result = asyncio.new_event_loop().run_until_complete(go())
    payload = json.loads(_text(result))
    assert payload["ok"] is True
    assert payload["value"] == "The Root Focus"


def test_call_resolve_sprite_manifest_fallback(fake_mod_root, cache_dir):
    """Full wiring: no HOI4 install + a committed manifest resolves a vanilla-only sprite."""
    (fake_mod_root / "tools" / "validation" / "vanilla_sprites.txt").write_text(
        "GFX_vanilla_only\n", encoding="utf-8"
    )
    srv = build_server(_settings(fake_mod_root, cache_dir))

    async def go():
        return await srv.call_tool("resolve_sprite", {"name": "GFX_vanilla_only"})

    payload = json.loads(_text(asyncio.new_event_loop().run_until_complete(go())))
    assert payload["ok"] is True
    assert payload["source"] == "vanilla_manifest"


def test_call_resolve_sprite_index_wins_over_manifest(fake_mod_root, cache_dir):
    """An indexed sprite resolves from the .gfx index even when the manifest also lists it."""
    (fake_mod_root / "tools" / "validation" / "vanilla_sprites.txt").write_text(
        "GFX_test_sprite_one\n", encoding="utf-8"
    )
    srv = build_server(_settings(fake_mod_root, cache_dir))

    async def go():
        return await srv.call_tool("resolve_sprite", {"name": "GFX_test_sprite_one"})

    payload = json.loads(_text(asyncio.new_event_loop().run_until_complete(go())))
    assert payload["ok"] is True
    assert payload.get("source") != "vanilla_manifest"
    assert payload["file"] is not None


def test_call_parse_string(server):
    async def go():
        return await server.call_tool("parse_string", {"text": "a = 1"})

    result = asyncio.new_event_loop().run_until_complete(go())
    payload = json.loads(_text(result))
    assert payload["ok"] is True
    assert payload["root"]["value"]["children"][0]["name"] == "a"


def test_call_parse_string_error(server):
    async def go():
        return await server.call_tool("parse_string", {"text": "a = {{{"})

    result = asyncio.new_event_loop().run_until_complete(go())
    payload = json.loads(_text(result))
    assert payload["ok"] is False
    assert "error" in payload


def test_call_parse_string_error_reports_line_column(server):
    async def go():
        return await server.call_tool("parse_string", {"text": "a = 1\nb = {{{"})

    result = asyncio.new_event_loop().run_until_complete(go())
    payload = json.loads(_text(result))
    assert payload["ok"] is False
    assert "error" in payload
    assert "at (2, 6)" in payload["error"]


def test_call_find_focuses_with_prereq(server):
    async def go():
        return await server.call_tool(
            "find_focuses",
            {"has_prereq": "TST_root", "limit": "1", "offset": 1},
        )

    result = asyncio.new_event_loop().run_until_complete(go())
    payload = json.loads(_text(result))
    assert payload["ok"] is True
    assert payload["partial"] is False
    assert payload["skipped_files"] == 0
    assert payload["skipped_records"] == 0
    assert payload["total"] == 2
    assert payload["returned"] == 1
    assert payload["truncated"] is False
    assert [m["id"] for m in payload["matches"]] == ["TST_branch_b"]


def test_call_validate_list_with_pagination(server, fake_mod_root):
    validator = fake_mod_root / "tools" / "validation" / "validate_alpha.py"
    validator.write_text('TITLE = "Alpha"\n', encoding="utf-8")

    async def go():
        return await server.call_tool("validate_list", {"limit": "1", "offset": 0})

    result = asyncio.new_event_loop().run_until_complete(go())
    payload = json.loads(_text(result))
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["returned"] == 1
    assert payload["validators"] == [
        {"name": "alpha", "title": "Alpha", "title_source": "scraped", "module": "validate_alpha"}
    ]


def test_call_check_equipment_variant(server, fake_mod_root):
    validation = fake_mod_root / "tools" / "validation"
    (validation / "equipment_module_slots.py").write_text(
        """
class Finding:
    line = 4
    kind = "unknown_slot"
    message = "unused"
    hull = "test_hull"


def build_equipment_index(units_dir):
    return object()


def check_created_variants(content, index):
    return []
""",
        encoding="utf-8",
    )
    equipment = fake_mod_root / "common" / "units" / "equipment"
    equipment.mkdir(parents=True)
    (equipment / "hulls.txt").write_text("test_hull = {}\n", encoding="utf-8")

    async def go():
        return await server.call_tool(
            "check_equipment_variant",
            {
                "text": "create_equipment_variant = { type = test_hull modules = {} }",
            },
        )

    payload = json.loads(_text(asyncio.new_event_loop().run_until_complete(go())))
    assert payload["ok"] is True
    assert payload["valid"] is True
    assert payload["issues"] == []


def test_call_check_encoding_with_pagination(server, fake_mod_root):
    txt = fake_mod_root / "common" / "national_focus" / "test.txt"
    txt.write_bytes(b"\xef\xbb\xbf" + txt.read_bytes())

    async def go():
        return await server.call_tool(
            "check_encoding",
            {
                "files": ["common/national_focus/test.txt"],
                "limit": "1",
                "offset": 0,
            },
        )

    result = asyncio.new_event_loop().run_until_complete(go())
    payload = json.loads(_text(result))
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["returned"] == 1
    assert payload["truncated"] is False


def test_call_generate_gfx_merge(server, fake_mod_root):
    tex = fake_mod_root / "gfx" / "test"
    tex.mkdir(parents=True)
    for stem in ("one", "three"):
        (tex / f"{stem}.dds").write_bytes(b"x")

    async def go():
        return await server.call_tool(
            "generate_gfx_merge",
            {
                "texture_dir": "gfx/test",
                "gfx_file": "interface/test_sprites.gfx",
                "prefix": "GFX_test_sprite_",
                "protected": ["GFX_test_tile"],
            },
        )

    result = asyncio.new_event_loop().run_until_complete(go())
    payload = json.loads(_text(result))
    assert payload["ok"] is True
    assert payload["new"] == ["GFX_test_sprite_three"]
    assert payload["orphaned"] == ["GFX_test_sprite_two"]
    assert 'name = "GFX_test_sprite_three"' in payload["txt"]
    assert payload["would_write"] is True


def test_resource_focus_raw(server):
    async def go():
        return await server.read_resource("md://focus/TST_root")

    contents = asyncio.new_event_loop().run_until_complete(go())
    text = contents[0].content if hasattr(contents[0], "content") else str(contents[0])
    assert "id = TST_root" in text
    assert "completion_reward" in text


def test_resource_loc_raw(server):
    async def go():
        return await server.read_resource("md://loc/TST_root")

    contents = asyncio.new_event_loop().run_until_complete(go())
    text = contents[0].content if hasattr(contents[0], "content") else str(contents[0])
    assert text == "The Root Focus"
