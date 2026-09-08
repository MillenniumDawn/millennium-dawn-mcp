from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from md_mcp.config import Settings
from md_mcp.server import build_server
from md_mcp.tools.lookup_docs import lookup_docs_tool
from md_mcp.util.response import BUDGET_BYTES


def _settings(mod_root, cache_dir) -> Settings:
    return Settings(
        mod_root=mod_root,
        vanilla_path=None,
        cache_dir=cache_dir,
        validator_mode="in_process",
        default_lang="en",
    )


def _write_docs(mod_root, kind: str, content: str) -> None:
    path = mod_root / "resources" / "documentation" / f"{kind}s_documentation.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_lookup_docs_signature() -> None:
    parameters = inspect.signature(lookup_docs_tool).parameters
    assert list(parameters) == ["settings", "kind", "key", "limit", "offset"]
    assert parameters["key"].default is None
    assert parameters["limit"].default == 100
    assert parameters["offset"].default == 0


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

    out = lookup_docs_tool(_settings(fake_mod_root, cache_dir), "effect", key="add_advisor_role")

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

    settings = _settings(fake_mod_root, cache_dir)
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

    out = lookup_docs_tool(_settings(fake_mod_root, cache_dir), "trigger", key="has_country_falg")

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
    settings = _settings(fake_mod_root, cache_dir)

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

    out = lookup_docs_tool(_settings(fake_mod_root, cache_dir), "effect", limit="1", offset=1.9)

    assert out["ok"] is True
    assert out["total"] == 3
    assert out["returned"] == 1
    assert out["truncated"] is True
    assert [entry["key"] for entry in out["entries"]] == ["second_effect"]


def test_lookup_docs_invalid_kind_budget(fake_mod_root, cache_dir) -> None:
    out = lookup_docs_tool(_settings(fake_mod_root, cache_dir), "x" * (BUDGET_BYTES + 1))

    assert out["ok"] is False
    assert out["size_truncated"] is True
    assert "kind" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_lookup_docs_budget_drops_oversized_definition(fake_mod_root, cache_dir) -> None:
    _write_docs(fake_mod_root, "effect", f"# Effects\n\n## huge_effect\n{'x' * 110_000}\n")

    out = lookup_docs_tool(_settings(fake_mod_root, cache_dir), "effect", key="huge_effect")

    assert out["ok"] is True
    assert out["size_truncated"] is True
    assert "entries" not in out
    assert out["entries_dropped"] == 1
    assert out["file"] == "resources/documentation/effects_documentation.md"
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_lookup_docs_missing_file(fake_mod_root, cache_dir) -> None:
    out = lookup_docs_tool(_settings(fake_mod_root, cache_dir), "modifier")

    assert out["ok"] is False
    assert out["file"] == "resources/documentation/modifiers_documentation.md"
    assert "not found" in out["error"].lower()


def test_lookup_docs_mcp_registration_and_call(fake_mod_root, cache_dir) -> None:
    _write_docs(fake_mod_root, "effect", "# Effects\n\n## test_effect\nA test effect.\n")
    server = build_server(_settings(fake_mod_root, cache_dir))

    async def go():
        return await server.list_tools(), await server.call_tool(
            "lookup_docs", {"kind": "effect", "key": "test_effect"}
        )

    tools, result = asyncio.run(go())
    assert "lookup_docs" in {tool.name for tool in tools}
    payload = json.loads(cast(Any, result)[0].text)
    assert payload["ok"] is True
    assert payload["entries"][0]["key"] == "test_effect"


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
            return await session.call_tool(
                "lookup_docs", {"kind": "effect", "key": "add_advisor_role"}
            )

    result = asyncio.run(go())
    payload = json.loads(cast(Any, result).content[0].text)
    assert payload["ok"] is True
    assert payload["entries"][0]["key"] == "add_advisor_role"


@pytest.mark.integration
def test_lookup_docs_real_checkout(real_mod_root, cache_dir) -> None:
    out = lookup_docs_tool(_settings(real_mod_root, cache_dir), "effect", key="add_advisor_role")

    assert out["ok"] is True
    assert out["entries"]
    assert out["entries"][0]["key"] == "add_advisor_role"
    assert out["entries"][0]["line"] > 0
    assert out["file"] == "resources/documentation/effects_documentation.md"
