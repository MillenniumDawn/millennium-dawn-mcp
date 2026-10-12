from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from md_mcp.server import build_server
from md_mcp.tools.lookup_docs import lookup_docs_tool
from md_mcp.util.response import BUDGET_BYTES
from tests.support.mod import in_process_settings


def _write_docs(mod_root, kind: str, content: str) -> None:
    path = mod_root / "resources" / "documentation" / f"{kind}s_documentation.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_system_doc(mod_root, tree: str, name: str, content: str) -> None:
    path = mod_root / ".claude" / tree / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_lookup_docs_exact_hit_excludes_toc_and_scope_headings(fake_mod_root, cache_dir) -> None:
    _write_docs(
        fake_mod_root,
        "effect",
        """# Effects

## Table of Content
* [add_advisor_role](#add_advisor_role)

## Effects for scope any
* [add_advisor_role](#add_advisor_role)

## add_advisor_role
* Supported Scopes: COUNTRY
* Supported Targets: none

```
Adds an advisor role.
```

## next_effect
* Supported Scopes: any
""",
    )

    out = lookup_docs_tool(
        in_process_settings(fake_mod_root, cache_dir), "effect", key="add_advisor_role"
    )

    assert out["ok"] is True
    assert out["kind"] == "effect"
    assert out["key"] == "add_advisor_role"
    assert out["total"] == 1
    assert out["returned"] == 1
    assert out["entries"][0]["key"] == "add_advisor_role"
    assert "Adds an advisor role." in out["entries"][0]["content"]
    assert out["file"] == "resources/documentation/effects_documentation.md"
    assert out["line"] == 9
    assert out["entries"][0]["line"] == 9


def test_lookup_docs_strips_html_spans_and_keeps_duplicate_definitions(
    fake_mod_root, cache_dir
) -> None:
    _write_docs(
        fake_mod_root,
        "modifier",
        """# Modifiers

## Modifiers for scope country

##  <span id="_cost_factor"></span><IdeaGroup>_cost_factor
* **Description**: First definition.

##  <span id="_cost_factor"></span><IdeaGroup>_cost_factor
* **Description**: Second definition.

## ordinary_modifier
* **Categories**: country
""",
    )

    settings = in_process_settings(fake_mod_root, cache_dir)
    exact = lookup_docs_tool(settings, "modifier", key="<IdeaGroup>_cost_factor")
    page = lookup_docs_tool(settings, "modifier", limit=1, offset=0)

    assert exact["ok"] is True
    assert exact["total"] == 2
    assert exact["returned"] == 2
    assert "First definition." in exact["entries"][0]["content"]
    assert "Second definition." in exact["entries"][1]["content"]
    assert page["total"] == 2
    assert [entry["key"] for entry in page["entries"]] == ["<IdeaGroup>_cost_factor"]
    assert page["entries"][0].get("content") is None


def test_lookup_docs_miss_has_close_match_suggestions(fake_mod_root, cache_dir) -> None:
    _write_docs(
        fake_mod_root,
        "trigger",
        """# Triggers

## has_country_flag
* Supported Scopes: COUNTRY

## has_idea
* Supported Scopes: COUNTRY
""",
    )

    out = lookup_docs_tool(
        in_process_settings(fake_mod_root, cache_dir), "trigger", key="has_country_falg"
    )

    assert out["ok"] is False
    assert out["key"] == "has_country_falg"
    assert out["total"] == 1
    assert out["returned"] == 1
    assert out["truncated"] is False
    assert out["suggestions"] == ["has_country_flag"]


def test_lookup_docs_paginates_miss_suggestions(fake_mod_root, cache_dir) -> None:
    _write_docs(
        fake_mod_root,
        "effect",
        "# Effects\n\n"
        "## test_effect_a\n\n"
        "## test_effect_b\n\n"
        "## test_effect_c\n\n"
        "## test_effect_d\n\n"
        "## test_effect_e\n",
    )
    settings = in_process_settings(fake_mod_root, cache_dir)

    all_suggestions = lookup_docs_tool(settings, "effect", key="test_effect", limit=5)
    page = lookup_docs_tool(settings, "effect", key="test_effect", limit=2, offset=1)

    assert all_suggestions["total"] == 5
    assert all_suggestions["returned"] == 5
    assert all_suggestions["truncated"] is False
    assert page["total"] == 5
    assert page["returned"] == 2
    assert page["truncated"] is True
    assert page["suggestions"] == all_suggestions["suggestions"][1:3]


def test_lookup_docs_paginates_key_list(fake_mod_root, cache_dir) -> None:
    _write_docs(
        fake_mod_root,
        "effect",
        """# Effects

## first_effect
first

## second_effect
second

## third_effect
third
""",
    )

    out = lookup_docs_tool(
        in_process_settings(fake_mod_root, cache_dir), "effect", limit="1", offset=1.9
    )

    assert out["ok"] is True
    assert out["total"] == 3
    assert out["returned"] == 1
    assert out["truncated"] is True
    assert [entry["key"] for entry in out["entries"]] == ["second_effect"]


def test_lookup_docs_invalid_kind_budget(fake_mod_root, cache_dir) -> None:
    out = lookup_docs_tool(in_process_settings(fake_mod_root, cache_dir), "x" * (BUDGET_BYTES + 1))

    assert out["ok"] is False
    assert out["size_truncated"] is True
    assert "kind" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_lookup_docs_budget_drops_oversized_definition(fake_mod_root, cache_dir) -> None:
    _write_docs(fake_mod_root, "effect", f"# Effects\n\n## huge_effect\n{'x' * 110_000}\n")

    out = lookup_docs_tool(
        in_process_settings(fake_mod_root, cache_dir), "effect", key="huge_effect"
    )

    assert out["ok"] is True
    assert out["size_truncated"] is True
    assert "entries" not in out
    assert out["entries_dropped"] == 1
    # issue #168: counts must reflect actually delivered entries, not pre-drop counts.
    assert out["returned"] == 0
    assert out["truncated"] is True
    assert out["file"] == "resources/documentation/effects_documentation.md"
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_lookup_docs_huge_entry_content_window_reassembles(fake_mod_root, cache_dir) -> None:
    """issue #168: sequential character windows reconstruct a single huge line exactly."""
    original = "x" * 110_000  # one huge line: line-only clipping would be useless here
    _write_docs(fake_mod_root, "effect", f"# Effects\n\n## huge_effect\n{original}\n")
    settings = in_process_settings(fake_mod_root, cache_dir)

    chunks: list[str] = []
    offset = 0
    while True:
        page = lookup_docs_tool(
            settings, "effect", key="huge_effect", content_offset=offset, content_limit=20_000
        )
        assert page["ok"] is True
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
        entry = page["entries"][0]
        assert entry["content_offset"] == offset
        assert entry["content_total"] == 110_000
        chunks.append(entry["content"])
        if entry["content_complete"]:
            assert entry["content_next_offset"] is None
            break
        assert entry["content_next_offset"] == offset + len(entry["content"])
        offset = entry["content_next_offset"]

    assert "".join(chunks) == original


def test_lookup_docs_content_window_shrinks_on_escaping_expansion(fake_mod_root, cache_dir) -> None:
    """The budget applies to serialised JSON: \x01 escapes need 6 bytes each."""
    original = "\x01" * 100_000
    _write_docs(fake_mod_root, "effect", f"# Effects\n\n## ctrl_effect\n{original}\n")
    settings = in_process_settings(fake_mod_root, cache_dir)

    page = lookup_docs_tool(
        settings, "effect", key="ctrl_effect", content_offset=0, content_limit=100_000
    )

    assert page["ok"] is True
    assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
    entry = page["entries"][0]
    # requested 100_000 characters, but \u0001 escapes need 6 bytes, so fewer delivered
    assert len(entry["content"]) < 100_000
    assert entry["content_next_offset"] == len(entry["content"])
    # continuation reconstructs the rest without gaps
    chunks = [entry["content"]]
    offset = entry["content_next_offset"]
    while offset is not None:
        rest = lookup_docs_tool(
            settings,
            "effect",
            key="ctrl_effect",
            content_offset=offset,
            content_limit=10_000,
        )
        next_entry = rest["entries"][0]
        chunks.append(next_entry["content"])
        offset = next_entry["content_next_offset"]
    assert "".join(chunks) == original


def test_lookup_docs_content_window_offsets_count_characters(fake_mod_root, cache_dir) -> None:
    """Offsets are code points, not bytes or lines: multi-byte chars stay intact."""
    original = "🚀" * 30_000 + "é" * 30_000
    _write_docs(fake_mod_root, "effect", f"# Effects\n\n## unicode_effect\n{original}\n")
    settings = in_process_settings(fake_mod_root, cache_dir)

    chunks: list[str] = []
    offset = 0
    while True:
        page = lookup_docs_tool(
            settings, "effect", key="unicode_effect", content_offset=offset, content_limit=25_000
        )
        assert page["ok"] is True
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
        entry = page["entries"][0]
        assert entry["content_total"] == 60_000
        chunks.append(entry["content"])
        if entry["content_complete"]:
            break
        offset = entry["content_next_offset"]

    reassembled = "".join(chunks)
    assert reassembled == original
    # never split a surrogate pair / multi-byte character
    assert reassembled.count("\ud83d") == 0


def test_lookup_docs_small_doc_default_shape_unchanged(fake_mod_root, cache_dir) -> None:
    _write_docs(fake_mod_root, "effect", "# Effects\n\n## small_effect\nSmall.\n")

    out = lookup_docs_tool(
        in_process_settings(fake_mod_root, cache_dir), "effect", key="small_effect"
    )

    assert out["ok"] is True
    assert "content_offset" not in out
    assert "content_limit" not in out
    assert set(out["entries"][0]) == {"key", "content", "file", "line", "end_line"}


def test_lookup_docs_content_window_multiple_definitions_page_across(
    fake_mod_root, cache_dir
) -> None:
    """Two huge definitions: budget delivers one per call; entry offset pages across."""
    body = "y" * 60_000
    _write_docs(
        fake_mod_root,
        "effect",
        f"# Effects\n\n## doubled_effect\n{body}\n\n## doubled_effect\nZ{body}\n",
    )
    settings = in_process_settings(fake_mod_root, cache_dir)

    first = lookup_docs_tool(settings, "effect", key="doubled_effect")
    assert first["ok"] is True
    assert first["total"] == 2
    assert len(json.dumps(first, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
    # Unwindowed entries retain the legacy shape; budget-dropped entries are counted truthfully.
    if "entries" in first:
        assert first["returned"] == len(first["entries"])
        assert all("content_complete" not in entry for entry in first["entries"])
    else:
        assert first["returned"] == 0
        assert first["truncated"] is True

    second = lookup_docs_tool(settings, "effect", key="doubled_effect", offset=1)
    assert second["entries"][0]["key"] == "doubled_effect"

    # with windows, definitions are retrieved one page at a time
    page0 = lookup_docs_tool(
        settings, "effect", key="doubled_effect", content_offset=0, content_limit=30_000
    )
    assert page0["returned"] >= 1
    e0 = page0["entries"][0]
    assert len(e0["content"]) == 30_000
    assert e0["content_next_offset"] == 30_000
    tail = lookup_docs_tool(
        settings, "effect", key="doubled_effect", content_offset=30_000, content_limit=30_000
    )
    assert e0["content"] + tail["entries"][0]["content"] == body


def test_lookup_docs_system_doc_content_window(fake_mod_root, cache_dir) -> None:
    body = "w" * 150_000
    original = f"# Massive\n\n{body}\n"
    _write_system_doc(fake_mod_root, "docs", "massive", original)
    settings = in_process_settings(fake_mod_root, cache_dir)

    page = lookup_docs_tool(settings, "doc", key="massive", content_offset=0)

    assert page["ok"] is True
    assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
    entry = page["entries"][0]
    assert entry["file"] == ".claude/docs/massive.md"
    assert entry["content_total"] == len(original)
    assert len(entry["content"]) < len(original)
    chunks = [entry["content"]]
    offset = entry["content_next_offset"]
    while offset is not None:
        tail = lookup_docs_tool(
            settings, "doc", key="massive", content_offset=offset, content_limit=10_000
        )
        tail_entry = tail["entries"][0]
        chunks.append(tail_entry["content"])
        offset = tail_entry["content_next_offset"]
    assert "".join(chunks) == original


@pytest.mark.parametrize(
    ("kind", "tree", "name", "original", "dropped"),
    [
        pytest.param(
            "doc",
            "docs",
            "large",
            "# " + "x" * 60_000 + "\nBody.\n",
            ["aliases", "title"],
            id="ascii-heading",
        ),
        pytest.param(
            "doc",
            "docs",
            "large",
            "# " + "🚀" * 30_000 + "\nBody.\n",
            ["aliases", "title"],
            id="unicode-heading",
        ),
        pytest.param(
            "doc",
            "docs",
            "large",
            "# " + "\x01" * 20_000 + "\nBody.\n",
            ["aliases", "title"],
            id="escaped-heading",
        ),
        pytest.param(
            "skill",
            "skills",
            "large/SKILL",
            "---\nname: large\ndescription: " + "x" * 60_000 + "\n---\nBody.\n",
            ["aliases", "title", "frontmatter", "description"],
            id="skill-frontmatter",
        ),
    ],
)
def test_lookup_docs_oversized_metadata_keeps_content_retrievable(
    fake_mod_root, cache_dir, kind, tree, name, original, dropped
) -> None:
    _write_system_doc(fake_mod_root, tree, name, original)
    settings = in_process_settings(fake_mod_root, cache_dir)
    chunks = []
    offset = 0
    for _ in range(20):
        page = lookup_docs_tool(
            settings, kind, key="large", content_offset=offset, content_limit=100_000
        )
        assert page["ok"] is True
        assert page["total"] == page["returned"] == 1
        assert page["truncated"] is False
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
        entry = page["entries"][0]
        assert entry["metadata_dropped"] == dropped
        assert all(field not in entry for field in dropped)
        assert entry["key"] == f".claude/{tree}/{name}"
        assert entry["file"] == f".claude/{tree}/{name}.md"
        assert entry["line"] == 1
        assert entry["end_line"] == len(original.splitlines())
        assert entry["content_total"] == len(original)
        assert entry["content_offset"] == offset
        chunks.append(entry["content"])
        if entry["content_complete"]:
            assert entry["content_next_offset"] is None
            break
        assert entry["content_next_offset"] > offset
        offset = entry["content_next_offset"]
    else:
        pytest.fail("metadata-bounded content continuation did not finish")
    assert "".join(chunks) == original


def test_lookup_docs_oversized_metadata_zero_window_keeps_provenance(
    fake_mod_root, cache_dir
) -> None:
    original = "# " + "x" * 60_000 + "\nBody.\n"
    _write_system_doc(fake_mod_root, "docs", "large", original)
    page = lookup_docs_tool(
        in_process_settings(fake_mod_root, cache_dir), "doc", key="large", content_limit=0
    )
    assert page["returned"] == 1
    entry = page["entries"][0]
    assert entry["metadata_dropped"] == ["aliases", "title"]
    assert entry["file"] == ".claude/docs/large.md"
    assert entry["content"] == ""
    assert entry["content_total"] == len(original)
    assert entry["content_complete"] is False
    assert entry["content_next_offset"] == 0
    assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_lookup_docs_small_window_keeps_optional_metadata(fake_mod_root, cache_dir) -> None:
    original = "---\nname: sample\ndescription: Short description\n---\n# Sample\nBody.\n"
    _write_system_doc(fake_mod_root, "skills", "sample/SKILL", original)
    settings = in_process_settings(fake_mod_root, cache_dir)
    full = lookup_docs_tool(settings, "skill", key="sample")["entries"][0]
    windowed = lookup_docs_tool(settings, "skill", key="sample", content_limit=5)["entries"][0]
    assert "metadata_dropped" not in windowed
    for field in (
        "aliases",
        "title",
        "frontmatter",
        "description",
        "key",
        "file",
        "line",
        "end_line",
    ):
        assert windowed[field] == full[field]
    assert windowed["content"] == original[:5]


def test_lookup_docs_content_window_edge_values(fake_mod_root, cache_dir) -> None:
    _write_docs(fake_mod_root, "trigger", "# Triggers\n\n## edge_effect\nabcdef\n")
    settings = in_process_settings(fake_mod_root, cache_dir)

    # non-integer values fail like limit/offset do
    bad = lookup_docs_tool(settings, "trigger", key="edge_effect", content_limit="abc")
    assert bad["ok"] is False
    assert "content_limit must be an integer" in bad["error"]

    # negatives clamp to 0 like other pagination bounds
    clamped = lookup_docs_tool(
        settings, "trigger", key="edge_effect", content_offset=-5, content_limit=-3
    )
    assert clamped["ok"] is True
    assert clamped["content_offset"] == 0
    assert clamped["content_limit"] == 0
    assert clamped["entries"][0]["content"] == ""
    assert clamped["entries"][0]["content_complete"] is False
    assert clamped["entries"][0]["content_next_offset"] == 0

    # offset past the end delivers an empty complete window
    past = lookup_docs_tool(
        settings, "trigger", key="edge_effect", content_offset=100, content_limit=5
    )
    assert past["entries"][0]["content"] == ""
    assert past["entries"][0]["content_complete"] is True
    assert past["entries"][0]["content_next_offset"] is None


def test_lookup_docs_content_window_zero_limit_delivers_provenance(
    fake_mod_root, cache_dir
) -> None:
    _write_docs(fake_mod_root, "modifier", "# Modifiers\n\n## probe_modifier\nxyz\n")

    out = lookup_docs_tool(
        in_process_settings(fake_mod_root, cache_dir),
        "modifier",
        key="probe_modifier",
        content_limit=0,
    )

    entry = out["entries"][0]
    assert entry["content"] == ""
    assert entry["content_offset"] == 0
    assert entry["content_total"] == 3
    assert entry["content_complete"] is False
    assert entry["content_next_offset"] == 0


def test_lookup_docs_missing_file(fake_mod_root, cache_dir) -> None:
    out = lookup_docs_tool(in_process_settings(fake_mod_root, cache_dir), "modifier")

    assert out["ok"] is False
    assert out["file"] == "resources/documentation/modifiers_documentation.md"
    assert "not found" in out["error"].lower()


def test_lookup_docs_exposes_claude_docs_and_rules_with_budgeted_pages(
    fake_mod_root, cache_dir
) -> None:
    _write_system_doc(fake_mod_root, "docs", "workflow", "# Workflow\n\nUse the safe workflow.\n")
    _write_system_doc(fake_mod_root, "rules", "content", "# Content Rules\n\nKeep edits scoped.\n")
    settings = in_process_settings(fake_mod_root, cache_dir)

    docs = lookup_docs_tool(settings, "claude_docs")
    exact = lookup_docs_tool(settings, "doc", key="workflow")
    rules = lookup_docs_tool(settings, "rules", key="content")

    assert docs["ok"] is True
    assert docs["total"] == 1
    assert docs["entries"][0]["file"] == ".claude/docs/workflow.md"
    assert exact["ok"] is True
    assert "Use the safe workflow." in exact["entries"][0]["content"]
    assert exact["file"] == ".claude/docs/workflow.md"
    assert rules["ok"] is True
    assert "Keep edits scoped." in rules["entries"][0]["content"]


def test_lookup_docs_missing_claude_tree_is_graceful(fake_mod_root, cache_dir) -> None:
    out = lookup_docs_tool(in_process_settings(fake_mod_root, cache_dir), "claude_rules")

    assert out["ok"] is False
    assert ".claude/rules" in out["file"]
    assert "not found" in out["error"].lower()


def test_lookup_docs_oversized_metadata_over_stdio(fake_mod_root, cache_dir) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    original = "# " + "x" * 60_000 + "\nBody.\n"
    _write_system_doc(fake_mod_root, "docs", "large", original)
    command = shutil.which("md-mcp")
    assert command is not None
    parameters = StdioServerParameters(
        command=command,
        args=["serve", "--mod-root", str(fake_mod_root)],
        env={
            **os.environ,
            "MD_MCP_CACHE_DIR": str(cache_dir),
            "MD_MCP_VALIDATOR_MODE": "isolated",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
    )

    async def go():
        chunks = []
        offset = 0
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            for _ in range(20):
                result = await session.call_tool(
                    "lookup_docs",
                    {
                        "kind": "doc",
                        "key": "large",
                        "content_offset": offset,
                        "content_limit": 20_000,
                    },
                )
                assert result.isError is False
                payload = json.loads(cast(Any, result).content[0].text)
                assert payload["returned"] == 1
                assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
                entry = payload["entries"][0]
                assert entry["metadata_dropped"] == ["aliases", "title"]
                assert entry["file"] == ".claude/docs/large.md"
                assert entry["content_offset"] == offset
                chunks.append(entry["content"])
                if entry["content_complete"]:
                    assert entry["content_next_offset"] is None
                    return "".join(chunks)
                assert entry["content_next_offset"] > offset
                offset = entry["content_next_offset"]
        pytest.fail("protocol content continuation did not finish")

    assert asyncio.run(go()) == original


def test_lookup_docs_mcp_registration_and_call(fake_mod_root, cache_dir) -> None:
    _write_docs(fake_mod_root, "effect", "# Effects\n\n## test_effect\nA test effect.\n")
    _write_system_doc(fake_mod_root, "docs", "workflow", "# Workflow\n\nUse the workflow.\n")
    server = build_server(in_process_settings(fake_mod_root, cache_dir))

    async def go():
        return (
            await server.list_tools(),
            await server.call_tool("lookup_docs", {"kind": "effect", "key": "test_effect"}),
            await server.call_tool("lookup_docs", {"kind": "claude_docs", "key": "workflow"}),
            await server.call_tool(
                "lookup_docs",
                {"kind": "effect", "key": "test_effect", "content_limit": 5},
            ),
        )

    tools, result, system_result, windowed_result = asyncio.run(go())
    description = next(tool.description for tool in tools if tool.name == "lookup_docs")
    assert description is not None
    for advertised in (
        "effect",
        "trigger",
        "modifier",
        ".claude/docs",
        ".claude/rules",
        ".claude/skills",
        "skill|skills",
        "content_offset",
        "content_limit",
    ):
        assert advertised in description
    payload = json.loads(cast(Any, result)[0].text)
    assert payload["ok"] is True
    assert payload["entries"][0]["key"] == "test_effect"
    system_payload = json.loads(cast(Any, system_result)[0].text)
    assert system_payload["ok"] is True
    assert "Use the workflow." in system_payload["entries"][0]["content"]
    windowed_payload = json.loads(cast(Any, windowed_result)[0].text)
    assert windowed_payload["ok"] is True
    assert windowed_payload["entries"][0]["content"] == "A tes"
    assert windowed_payload["entries"][0]["content_complete"] is False


@pytest.mark.integration
def test_lookup_docs_real_mcp_stdio(real_mod_root, cache_dir) -> None:
    command = shutil.which("md-mcp")
    if command is None:
        pytest.skip("md-mcp executable not installed")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command=command,
        args=["serve", "--mod-root", str(real_mod_root)],
        env={
            **os.environ,
            "MD_MCP_CACHE_DIR": str(cache_dir),
            "MD_MCP_VALIDATOR_MODE": "isolated",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
    )

    async def go():
        async with (
            stdio_client(server_params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            exact = await session.call_tool(
                "lookup_docs", {"kind": "effect", "key": "add_advisor_role"}
            )
            windowed = await session.call_tool(
                "lookup_docs",
                {
                    "kind": "effect",
                    "key": "add_advisor_role",
                    "content_offset": 5,
                    "content_limit": 40,
                },
            )
            guide_chunks = []
            content_offset = 0
            for _ in range(100):
                guide_page = await session.call_tool(
                    "lookup_docs",
                    {
                        "kind": "doc",
                        "key": "validation-pipeline",
                        "content_offset": content_offset,
                        "content_limit": 4_000,
                    },
                )
                guide_payload = json.loads(cast(Any, guide_page).content[0].text)
                guide_entry = guide_payload["entries"][0]
                guide_chunks.append(guide_entry["content"])
                if guide_entry["content_complete"]:
                    break
                content_offset = guide_entry["content_next_offset"]
            else:
                pytest.fail("validation-pipeline protocol continuation exceeded 100 pages")
            return exact, windowed, "".join(guide_chunks)

    exact, windowed, guide_content = asyncio.run(go())
    payload = json.loads(cast(Any, exact).content[0].text)
    assert payload["ok"] is True
    assert payload["entries"][0]["key"] == "add_advisor_role"
    windowed_payload = json.loads(cast(Any, windowed).content[0].text)
    assert windowed_payload["ok"] is True
    entry = windowed_payload["entries"][0]
    assert entry["content_offset"] == 5
    assert entry["content_total"] == len(payload["entries"][0]["content"])
    assert entry["content"] == payload["entries"][0]["content"][5:45]
    reference = (real_mod_root / ".claude/docs/validation-pipeline.md").read_text(encoding="utf-8")
    assert guide_content == reference


@pytest.mark.integration
def test_lookup_docs_real_checkout(real_mod_root, cache_dir) -> None:
    out = lookup_docs_tool(
        in_process_settings(real_mod_root, cache_dir), "effect", key="add_advisor_role"
    )

    assert out["ok"] is True
    assert out["entries"]
    assert out["entries"][0]["key"] == "add_advisor_role"
    assert out["entries"][0]["line"] > 0
    assert out["file"] == "resources/documentation/effects_documentation.md"
    # windowed retrieval matches the full content on real data
    windowed = lookup_docs_tool(
        in_process_settings(real_mod_root, cache_dir),
        "effect",
        key="add_advisor_role",
        content_offset=2,
        content_limit=10,
    )
    assert windowed["entries"][0]["content"] == out["entries"][0]["content"][2:12]
    assert windowed["entries"][0]["content_total"] == len(out["entries"][0]["content"])


@pytest.mark.integration
def test_lookup_docs_real_validation_pipeline_guide(real_mod_root, cache_dir) -> None:
    """Probe the real .claude/docs/validation-pipeline.md guide through lookup_docs."""
    settings = in_process_settings(real_mod_root, cache_dir)
    reference = (real_mod_root / ".claude/docs/validation-pipeline.md").read_text(encoding="utf-8")
    assert "validator" in reference.lower()

    # Page the guide in character windows and reconstruct it without gaps.
    chunks = []
    offset = 0
    for _ in range(50):
        page = lookup_docs_tool(
            settings, "doc", key="validation-pipeline", content_offset=offset, content_limit=1_000
        )
        assert page["ok"] is True
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
        entry = page["entries"][0]
        assert entry["content_offset"] == offset
        assert (
            len(entry["content"]) == min(1_000, entry["content_total"] - offset)
            or entry["content_complete"]
        )
        chunks.append(entry["content"])
        if entry["content_complete"]:
            break
        offset = entry["content_next_offset"]
    else:
        pytest.fail("content window continuation did not terminate within 50 pages")

    assert "".join(chunks) == reference


@pytest.mark.integration
def test_lookup_docs_real_longest_definition_reassembles(real_mod_root, cache_dir) -> None:
    """Windowed continuation reconstructs the longest real documentation entry."""
    settings = in_process_settings(real_mod_root, cache_dir)
    listing = lookup_docs_tool(settings, "effect")
    assert listing["ok"] is True
    longest = max(listing["entries"], key=lambda entry: entry["end_line"] - entry["line"])
    key = longest["key"]

    chunks = []
    offset = 0
    calls = 0
    while True:
        calls += 1
        page = lookup_docs_tool(
            settings, "effect", key=key, content_offset=offset, content_limit=4_000
        )
        assert page["ok"] is True
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
        entry = page["entries"][0]
        chunks.append(entry["content"])
        if entry["content_complete"]:
            break
        offset = entry["content_next_offset"]
        assert calls < 200

    reassembled = "".join(chunks)
    assert len(reassembled) == page["entries"][0]["content_total"]
    # The default (unwindowed) hit for the same key is either small enough to
    # return whole or dropped by the size guard — both acceptable, but the
    # reassembled length must match the real upstream content length.
    full = lookup_docs_tool(settings, "effect", key=key)
    assert full["ok"] is True
    if "entries" in full:
        assert len(full["entries"][0]["content"]) == len(reassembled)


def test_lookup_docs_system_key_miss_suggests_close_matches(fake_mod_root, cache_dir) -> None:
    _write_system_doc(fake_mod_root, "docs", "workflow", "# Workflow\nUse the safe workflow.\n")
    _write_system_doc(fake_mod_root, "docs", "workloads", "# Workloads\nOther content.\n")

    result = lookup_docs_tool(
        in_process_settings(fake_mod_root, cache_dir), "docs", key="workflow2"
    )

    assert result["ok"] is False
    assert "No docs documentation found for 'workflow2'" in result["error"]
    assert ".claude/docs/workflow" in result["suggestions"]


def test_lookup_docs_system_undecodable_document_is_graceful(fake_mod_root, cache_dir) -> None:
    bad = fake_mod_root / ".claude" / "docs" / "broken.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\xff\xfe # broken bytes")

    result = lookup_docs_tool(in_process_settings(fake_mod_root, cache_dir), "docs")

    assert result["ok"] is False
    assert "Could not read documentation" in result["error"]


def test_read_system_documents_skips_directories_named_like_docs(fake_mod_root, cache_dir) -> None:
    _write_system_doc(fake_mod_root, "docs", "workflow", "# Workflow\nReal content.\n")
    directory = fake_mod_root / ".claude" / "docs"
    (directory / "fake.md").mkdir(exist_ok=True)

    result = lookup_docs_tool(in_process_settings(fake_mod_root, cache_dir), "docs")

    assert result["ok"] is True
    keys = [entry["key"] for entry in result["entries"]]
    assert keys == [".claude/docs/workflow"]


def test_lookup_docs_lists_skill_frontmatter_and_references(fake_mod_root, cache_dir) -> None:
    skill = fake_mod_root / ".claude" / "skills" / "build" / "SKILL.md"
    reference = skill.parent / "references" / "guide.md"
    reference.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: Build skill\ndescription: Build safely\n---\n# Build\nBody\n", encoding="utf-8"
    )
    reference.write_text("# Guide\nReference body\n", encoding="utf-8")
    settings = in_process_settings(fake_mod_root, cache_dir)

    listing = lookup_docs_tool(settings, "skills")
    assert listing["ok"] is True
    assert listing["total"] == 2
    assert {entry["key"] for entry in listing["entries"]} == {
        ".claude/skills/build/SKILL",
        ".claude/skills/build/references/guide",
    }
    detail = lookup_docs_tool(settings, "skill", key="Build skill")
    assert detail["ok"] is True
    assert detail["entries"][0]["frontmatter"]["description"] == "Build safely"
    assert lookup_docs_tool(settings, "skills", key="build/guide")["ok"] is True
    assert lookup_docs_tool(settings, "skills", key="build/guide.md")["ok"] is True


def test_skill_frontmatter_folded_description_and_escaped_quote(fake_mod_root, cache_dir) -> None:
    skill = fake_mod_root / ".claude" / "skills" / "diary" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        (
            "---\nname: Diary\ndescription: >-\n"
            "  Write a country''s diary\n  with care.\n---\n# Diary\n"
        ),
        encoding="utf-8",
    )
    result = lookup_docs_tool(in_process_settings(fake_mod_root, cache_dir), "skill", key="diary")
    assert result["entries"][0]["description"] == "Write a country's diary with care."


def test_skill_frontmatter_unquotes_single_and_double_quoted_values(
    fake_mod_root, cache_dir
) -> None:
    skill = fake_mod_root / ".claude" / "skills" / "quoted" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: 'Quoted skill'\ndescription: \"It's quoted\"\n---\n# Quoted\n",
        encoding="utf-8",
    )

    result = lookup_docs_tool(in_process_settings(fake_mod_root, cache_dir), "skill", key="quoted")

    assert result["entries"][0]["frontmatter"] == {
        "name": "Quoted skill",
        "description": "It's quoted",
    }


def test_lookup_docs_undecodable_script_documentation_is_graceful(fake_mod_root, cache_dir) -> None:
    path = fake_mod_root / "resources" / "documentation" / "effects_documentation.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe # broken bytes")

    result = lookup_docs_tool(in_process_settings(fake_mod_root, cache_dir), "effect")

    assert result["ok"] is False
    assert result["file"] == "resources/documentation/effects_documentation.md"


def test_skill_lookup_by_directory_name_without_frontmatter(fake_mod_root, cache_dir) -> None:
    skill = fake_mod_root / ".claude" / "skills" / "plain" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Plain skill\nBody copy.\n", encoding="utf-8")

    result = lookup_docs_tool(in_process_settings(fake_mod_root, cache_dir), "skill", key="plain")

    assert result["ok"] is True
    assert result["entries"][0]["file"] == ".claude/skills/plain/SKILL.md"
    assert "frontmatter" not in result["entries"][0]


def test_skill_key_resolves_only_skill_doc_not_references(fake_mod_root, cache_dir) -> None:
    skill = fake_mod_root / ".claude" / "skills" / "build" / "SKILL.md"
    reference = skill.parent / "references" / "guide.md"
    reference.parent.mkdir(parents=True)
    skill.write_text("---\nname: build\n---\n# Build\nBody\n", encoding="utf-8")
    reference.write_text("# Guide\nReference body\n", encoding="utf-8")
    settings = in_process_settings(fake_mod_root, cache_dir)

    hit = lookup_docs_tool(settings, "skill", key="build")

    assert hit["ok"] is True
    assert hit["total"] == 1
    assert hit["entries"][0]["file"] == ".claude/skills/build/SKILL.md"

    resolved = lookup_docs_tool(settings, "skill", key="build/guide")

    assert resolved["ok"] is True
    assert resolved["entries"][0]["file"] == ".claude/skills/build/references/guide.md"


def test_skill_unterminated_frontmatter_is_not_parsed(fake_mod_root, cache_dir) -> None:
    skill = fake_mod_root / ".claude" / "skills" / "broken" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        (
            "---\nname: broken\ndescription: desc line\n"
            "# Broken skill\nname: leaked-from-body\nBody copy.\n"
        ),
        encoding="utf-8",
    )
    settings = in_process_settings(fake_mod_root, cache_dir)

    result = lookup_docs_tool(settings, "skill", key="broken")

    assert result["ok"] is True
    entry = result["entries"][0]
    assert "frontmatter" not in entry
    assert entry["title"] == "Broken skill"
    assert lookup_docs_tool(settings, "skill", key="leaked-from-body")["ok"] is False
