"""MCP protocol round-trip tests.

Builds the server with FastMCP's in-process API and calls every tool and
resource through it. Tool logic is tested directly in each tool's own module.
This file covers what only the server adds: registration, the parameters
agents see, argument forwarding, server-owned state, and the worker pool.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, cast

import pytest

from md_mcp import server as server_module
from md_mcp.config import Settings
from md_mcp.server import build_server
from tests.support.mod import in_process_settings


def _call(server, name: str, arguments: dict) -> dict:
    """Call a tool through FastMCP and decode its JSON payload."""
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result[0].text)


@pytest.fixture
def server_factory():
    instances = []

    def create(settings):
        instance = build_server(settings)
        instances.append(instance)
        return instance

    try:
        yield create
    finally:
        for instance in instances:
            cast(Any, instance)._md_blocking_tools.close()


@pytest.fixture
def server(fake_mod_root, cache_dir, server_factory):
    return server_factory(in_process_settings(fake_mod_root, cache_dir))


@pytest.fixture(scope="module")
def shared_server(shared_fake_mod_root):
    """One server for the tables below, whose calls never write to the mod.

    Building a server per row costs about 45 ms a row: 2.8 s for the tables, against 0.4 s shared.
    """
    cache_dir = shared_fake_mod_root.parent / ".md-mcp-cache"
    with pytest.MonkeyPatch.context() as patch:
        # As under `md-mcp serve`: this server's worker threads outlive a row,
        # so an index build must never fork while it is up.
        patch.setenv("MD_MCP_SERIAL_PARSE", "1")
        instance = build_server(in_process_settings(shared_fake_mod_root, cache_dir))
        yield instance
        cast(Any, instance)._md_blocking_tools.close()


def _overlay_settings(mod_root, cache_dir, submod_root) -> Settings:
    return Settings(
        mod_root=mod_root,
        vanilla_path=None,
        cache_dir=cache_dir,
        validator_mode="in_process",
        default_lang="en",
        submod_root=submod_root,
    )


# Every registered tool with the parameters an agent sees. A bare name is
# required and `name=default` is optional. Server-owned arguments (settings,
# indexes, the `kind` of a definition finder) must not appear.
TOOL_SIGNATURES = {
    "resolve_focus": "focus_id",
    "resolve_loc": "key, lang=None",
    "resolve_sprite": "name",
    "resolve_event": "event_id",
    "resolve_decision": "decision_id",
    "resolve_idea": "idea_id",
    "resolve_country_tag": "tag",
    "resolve_character": "character_id",
    "resolve_trait": "trait_id",
    "resolve_scripted_effect": "effect_id",
    "resolve_scripted_trigger": "trigger_id",
    "parse_file": "path, max_bytes=500000, top_level_only=False",
    "parse_string": "text",
    "find_focuses": "tag=None, has_prereq=None, mutex_with=None, kind=None, limit=200, offset=0",
    "find_country_tags": "query=None, limit=200, offset=0",
    "find_characters": "query=None, limit=200, offset=0",
    "find_traits": "query=None, limit=200, offset=0",
    "find_scripted_effects": "query=None, limit=200, offset=0",
    "find_scripted_triggers": "query=None, limit=200, offset=0",
    "lookup_docs": "kind, key=None, limit=100, offset=0, content_offset=None, content_limit=None",
    "find_references": "kind, target, limit=100, offset=0, snippet_chars=120, files_only=False",
    "list_country_content": "tag, include=None, limit_per_category=100",
    "check_equipment_variant": "text, limit=100, offset=0",
    "validate": (
        "validator=None, staged_only=False, files=None, strict=False, severity_min='info', "
        "limit=500, counts_only=False, delta=False, baseline=None"
    ),
    "validate_list": "limit=200, offset=0",
    "lint": (
        "mode='changed', files=None, checks=None, validators=None, severity_min='info', "
        "limit=500, counts_only=False"
    ),
    "review_branch": "base='main'",
    "fix_lint": "fixer, path=None, content=None",
    "generate_focus": (
        "id, tag, x=0, y=1, cost=10, icon=None, relative_position_id=None, "
        "prerequisites=None, mutually_exclusive=None, search_filters=None, available=None, "
        "completion_reward=None, ai_base=1, title=None, description=None"
    ),
    "generate_event": (
        "namespace, number, kind='country_event', is_triggered_only=True, "
        "fire_only_once=False, picture=None, trigger=None, immediate=None, options=None, "
        "title=None, description=None"
    ),
    "generate_decision": (
        "id, tag=None, icon=None, cost=25, days_remove=None, days_re_enable=None, "
        "allowed=None, visible=None, available=None, complete_effect=None, "
        "remove_effect=None, cancel_trigger=None, state_target=False, "
        "target_root_trigger=None, target_trigger=None, title=None, description=None"
    ),
    "generate_idea": (
        "id, tag=None, picture=None, modifier=None, research_bonus=None, "
        "equipment_bonus=None, targeted_modifier=None, allowed=None, available=None, "
        "cancel=None, cost=None, removal_cost=-1, title=None, description=None"
    ),
    "generate_gfx_entry": (
        "name, texturefile, kind='spriteType', frames=None, legacy_lazy_load=False"
    ),
    "generate_gfx_merge": (
        "texture_dir, gfx_file, prefix, kind='spriteType', frames=None, "
        "legacy_lazy_load=False, protected=None, limit=100, offset=0, include_file=False"
    ),
    "generate_loc_stub": "keys, lang='l_english', include_header=True, bom_prefix=False",
    "generate_missing_loc": (
        "focus_ids=None, event_ids=None, lang='en', limit=200, offset=0, bom_prefix=False"
    ),
    "standardize": "content=None, path=None, content_type=None",
    "tick_audit": "tag=None, limit=20, offset=0",
    "tick_spot_check": "files, scope=None, limit=100, offset=0",
    "estimate_gdp": "tag",
    "event_load": "tag='USA', window=45, threshold=3, limit=20, offset=0",
    "summarize_game_log": (
        "path, top=15, limit=100, offset=0, countries=None, since=None, until=None"
    ),
    "ai_path_report": "tag, section=None, limit=15, offset=0",
    "validation_timing_summary": "content, limit=20, offset=0, include_steps=False",
    "calculate_days": "year, month, day",
    "focus_graph": (
        "tag, detail='summary', focus_ids=None, node_limit=100, edge_limit=500, "
        "include_edges=True, include_nodes=True"
    ),
    "check_refs": "tag=None, files=None, kinds=None, limit=200, offset=0, counts_only=False",
    "focus_layout": "tag=None, file=None, include_positions=False, limit=300, offset=0",
    "render_focus_tree": (
        "tree=None, file=None, tag=None, country=None, focus_ids=None, region=None, margin=2, "
        "hide_branches=None, highlight=None, labels='both', format='png', scale=None, ref=None, "
        "limit=20"
    ),
    "focus_overlap_scenarios": (
        "file, tag, completed=None, date=None, include_positions=False, limit=50, offset=0"
    ),
    "diff_summary": "base='main', kinds=None, with_ids=True, limit=200",
    "check_encoding": "files=None, limit=200, offset=0",
    "check_attribution_labels": "content, limit=50, offset=0",
}


def _signature(tool) -> str:
    schema = tool.inputSchema
    required = set(schema.get("required", ()))
    return ", ".join(
        name if name in required else f"{name}={prop.get('default')!r}"
        for name, prop in schema["properties"].items()
    )


def test_tool_signatures(shared_server):
    tools = asyncio.run(shared_server.list_tools())

    assert {tool.name: _signature(tool) for tool in tools} == TOOL_SIGNATURES


@pytest.mark.parametrize(
    ("tool_name", "arguments", "helper_name", "expected_args", "expected_kwargs"),
    [
        (
            "tick_audit",
            {"tag": "TST", "limit": 7, "offset": 2},
            "tick_audit_tool",
            (),
            {"tag": "TST", "limit": 7, "offset": 2},
        ),
        ("estimate_gdp", {"tag": "TST"}, "estimate_gdp_tool", ("TST",), {}),
        (
            "summarize_game_log",
            {
                "path": "/tmp/game.log",
                "top": 4,
                "limit": 8,
                "offset": 3,
                "countries": ["Testland"],
                "since": "1936.1.1",
                "until": "1936.2.1",
            },
            "game_log_summary_tool",
            ("/tmp/game.log",),
            {
                "top": 4,
                "limit": 8,
                "offset": 3,
                "countries": ["Testland"],
                "since": "1936.1.1",
                "until": "1936.2.1",
            },
        ),
    ],
)
def test_analysis_wrappers_offload_and_forward_arguments(
    server, monkeypatch, tool_name, arguments, helper_name, expected_args, expected_kwargs
):
    started = threading.Event()
    release = threading.Event()
    forwarded: dict[str, Any] = {}

    def slow_helper(mod_root, *args, **kwargs):
        forwarded.update(mod_root=mod_root, args=args, kwargs=kwargs)
        started.set()
        assert release.wait(5), "test did not release the blocking helper"
        return {"ok": True, "tool": tool_name}

    monkeypatch.setattr(server_module, helper_name, slow_helper)

    async def exercise():
        slow = asyncio.create_task(server.call_tool(tool_name, arguments))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            fast = await asyncio.wait_for(
                server.call_tool("calculate_days", {"year": 2000, "month": 1, "day": 1}),
                timeout=2,
            )
            assert json.loads(fast[0].text) == {"ok": True, "days": 0}
            assert not slow.done()
        finally:
            release.set()
        result = await slow
        assert json.loads(result[0].text) == {"ok": True, "tool": tool_name}

    asyncio.run(exercise())
    assert forwarded["mod_root"] is not None
    assert forwarded["args"] == expected_args
    assert forwarded["kwargs"] == expected_kwargs


def _found(ident: str, kind: str, file: str, line: int, **extra) -> dict:
    return {
        "ok": True,
        "total": 1,
        "matches": [{"id": ident, "kind": kind, "file": file, "line": line, **extra}],
    }


# One call per tool against the fake mod: (arguments, top-level fields the
# payload must carry). The fields show that the arguments reached the tool and,
# for index-backed tools, that the right index did. A tool whose upstream script
# the fake mod lacks answers with a shaped error; the tests after the tables
# cover the mod root and the rest of the server's own state.
TOOL_CALLS: dict[str, tuple[dict, dict]] = {
    "resolve_focus": (
        {"focus_id": "TST_root"},
        {"ok": True, "id": "TST_root", "file": "common/national_focus/test.txt"},
    ),
    "resolve_loc": ({"key": "TST_root"}, {"ok": True, "value": "The Root Focus"}),
    "resolve_sprite": (
        {"name": "GFX_test_sprite_one"},
        {"ok": True, "texturefile": "gfx/test/one.dds"},
    ),
    "resolve_event": ({"event_id": "Testns.1"}, {"ok": True, "namespace": "Testns"}),
    "resolve_decision": (
        {"decision_id": "TST_simple_decision"},
        {"ok": True, "category": "TST_category"},
    ),
    "resolve_idea": ({"idea_id": "TST_simple_idea"}, {"ok": True, "category": "country"}),
    "resolve_country_tag": (
        {"tag": "TST"},
        {"ok": True, "country_file": "countries/Testland.txt"},
    ),
    "resolve_character": (
        {"character_id": "TST_test_character"},
        {"ok": True, "kind": "character"},
    ),
    "resolve_trait": ({"trait_id": "TST_test_trait"}, {"ok": True, "kind": "trait"}),
    "resolve_scripted_effect": (
        {"effect_id": "TST_test_effect"},
        {"ok": True, "kind": "scripted_effect"},
    ),
    "resolve_scripted_trigger": (
        {"trigger_id": "TST_test_trigger"},
        {"ok": True, "kind": "scripted_trigger"},
    ),
    "parse_file": (
        {"path": "common/national_focus/test.txt", "top_level_only": True},
        {
            "ok": True,
            "top_level": [{"name": "focus_tree", "line": 1}, {"name": "shared_focus", "line": 54}],
        },
    ),
    "parse_string": ({"text": "a = 1"}, {"ok": True}),
    # Numeric strings must pass the protocol's argument validation.
    "find_focuses": (
        {"has_prereq": "TST_root", "limit": "1", "offset": 1},
        {
            "ok": True,
            "partial": False,
            "skipped_files": 0,
            "skipped_records": 0,
            "total": 2,
            "returned": 1,
            "truncated": False,
            "matches": [
                {
                    "id": "TST_branch_b",
                    "file": "common/national_focus/test.txt",
                    "line": 39,
                    "kind": "focus_tree",
                }
            ],
        },
    ),
    "find_country_tags": (
        {"query": "TST"},
        _found(
            "TST",
            "country_tag",
            "common/country_tags/test_tags.txt",
            1,
            country_file="countries/Testland.txt",
        ),
    ),
    "find_characters": (
        {"query": "test", "limit": 1},
        _found("TST_test_character", "character", "common/characters/TST.txt", 2),
    ),
    "find_traits": (
        {"query": "test"},
        _found("TST_test_trait", "trait", "common/country_leader/TST_traits.txt", 2),
    ),
    "find_scripted_effects": (
        {"query": "test"},
        _found("TST_test_effect", "scripted_effect", "common/scripted_effects/test_effects.txt", 1),
    ),
    "find_scripted_triggers": (
        {"query": "test"},
        _found(
            "TST_test_trigger", "scripted_trigger", "common/scripted_triggers/test_triggers.txt", 1
        ),
    ),
    "lookup_docs": ({"kind": "doc"}, {"ok": False, "file": ".claude/docs"}),
    "find_references": ({"kind": "focus", "target": "TST_root"}, {"ok": True, "total": 2}),
    # One sample per index shows each index reached its own parameter.
    "list_country_content": (
        {"tag": "TST"},
        {
            "ok": True,
            "focuses_sample": ["TST_branch_a", "TST_branch_b", "TST_root", "TST_shared"],
            "decisions_sample": ["TST_simple_decision", "TST_targeted_decision"],
            "ideas_sample": ["TST_acme_tanks", "TST_another_idea", "TST_simple_idea"],
            "country_tags_sample": ["TST"],
            "characters_sample": ["TST_test_character"],
            "traits_sample": ["TST_test_trait"],
            "scripted_effects_sample": ["TST_test_effect"],
            "scripted_triggers_sample": ["TST_test_trigger"],
        },
    ),
    "check_equipment_variant": (
        {"text": "create_equipment_variant = { type = x }"},
        {"ok": False, "error": "create_equipment_variant requires a modules block."},
    ),
    "validate": ({}, {"ok": True, "validators": []}),
    "validate_list": ({"limit": "1", "offset": 0}, {"ok": True, "total": 0, "returned": 0}),
    "lint": (
        {"mode": "all", "validators": []},
        {"ok": False, "mode": "all", "validators_run": []},
    ),
    "review_branch": ({}, {"ok": False, "base": "main"}),
    "fix_lint": ({"fixer": "styling", "content": "a\n"}, {"ok": False, "fixer": "styling"}),
    "generate_focus": (
        {"id": "TST_new", "tag": "TST"},
        {
            "loc_yml_keys": [
                {"key": "TST_new", "value": "New"},
                {"key": "TST_new_desc", "value": "TODO: focus description."},
            ]
        },
    ),
    "generate_event": (
        {"namespace": "Testns", "number": 2},
        {"namespace_directive": "add_namespace = Testns"},
    ),
    "generate_decision": (
        {"id": "TST_new_decision"},
        {
            "loc_yml_keys": [
                {"key": "TST_new_decision", "value": "Tst New Decision"},
                {"key": "TST_new_decision_desc", "value": "TODO: decision description."},
            ]
        },
    ),
    "generate_idea": (
        {"id": "TST_new_idea"},
        {"txt": "\t\tTST_new_idea = {\n\t\t\tpicture = generic_idea\n\t\t}"},
    ),
    "generate_gfx_entry": (
        {"name": "GFX_new", "texturefile": "gfx/new.dds"},
        {"txt": '\tspriteType = {\n\t\tname = "GFX_new"\n\t\ttexturefile = "gfx/new.dds"\n\t}'},
    ),
    "generate_gfx_merge": (
        {
            "texture_dir": "gfx/test",
            "gfx_file": "interface/test_sprites.gfx",
            "prefix": "GFX_test_sprite_",
        },
        {"ok": False, "error": "texture_dir is not a directory: gfx/test"},
    ),
    "generate_loc_stub": (
        {"keys": [{"key": "TST_new", "value": "New"}]},
        {"txt": 'l_english:\n TST_new: "New"\n'},
    ),
    "generate_missing_loc": (
        {"focus_ids": ["TST_unindexed"]},
        {"ok": True, "complete": False, "partial": True},
    ),
    "standardize": (
        {"content": "focus = { id = TST_new }\n", "content_type": "focus"},
        {"ok": False},
    ),
    "tick_audit": ({}, {"ok": False}),
    "tick_spot_check": ({"files": ["events/test.txt"]}, {"ok": False}),
    "estimate_gdp": ({"tag": "TST"}, {"ok": False}),
    "event_load": ({}, {"ok": False}),
    "summarize_game_log": (
        {"path": "relative.log"},
        {"ok": False, "error": "path must be an absolute path"},
    ),
    "ai_path_report": ({"tag": "TST"}, {"ok": False}),
    "validation_timing_summary": (
        {"content": "{}", "include_steps": True},
        {"ok": False, "unsupported": True},
    ),
    "calculate_days": ({"year": 2001, "month": 1, "day": 1}, {"ok": True, "days": 365}),
    "focus_graph": (
        {"tag": "TST"},
        {"ok": True, "node_count": 4, "roots": ["TST_root", "TST_shared"]},
    ),
    "check_refs": ({"tag": "TST"}, {"ok": True, "files_scanned": 1, "total_unresolved": 4}),
    "focus_layout": ({"tag": "TST"}, {"ok": True, "focus_count": 3}),
    "render_focus_tree": (
        {"tag": "TST", "format": "html"},
        {"ok": True, "country": "TST", "source": "working tree"},
    ),
    "focus_overlap_scenarios": (
        {"file": "common/national_focus/test.txt", "tag": "TST"},
        {
            "ok": False,
            "error": "This Millennium-Dawn checkout has no focus overlap analysis helper.",
        },
    ),
    # The fake mod is not a git repository.
    "diff_summary": ({}, {"ok": False, "error": "git diff failed — is this a git repo?"}),
    "check_encoding": (
        {"files": ["common/national_focus/test.txt"], "limit": "1", "offset": 0},
        {"ok": True, "checked": 1, "total": 0},
    ),
    "check_attribution_labels": (
        {"content": "No explicit attribution label here."},
        {"ok": False, "unsupported": True},
    ),
}


@pytest.mark.parametrize("name", TOOL_SIGNATURES)
def test_tool_round_trips(shared_server, name):
    arguments, expected = TOOL_CALLS[name]

    payload = _call(shared_server, name, arguments)

    assert {key: payload.get(key) for key in expected} == expected


@pytest.mark.parametrize(
    ("uri", "fragment"),
    [
        ("md://focus/TST_root", "id = TST_root"),
        ("md://loc/TST_root", "The Root Focus"),
        ("md://sprite/GFX_test_sprite_one", 'name = "GFX_test_sprite_one"'),
        ("md://event/Testns.1", "id = Testns.1"),
        ("md://decision/TST_simple_decision", "TST_simple_decision = {"),
        ("md://idea/TST_simple_idea", "TST_simple_idea = {"),
    ],
)
def test_resource_round_trips(shared_server, uri, fragment):
    contents = asyncio.run(shared_server.read_resource(uri))

    text = contents[0].content if hasattr(contents[0], "content") else str(contents[0])
    assert fragment in text


@pytest.mark.parametrize(
    ("name", "missing"),
    [
        ("standardize", "standardization"),
        ("tick_audit", "tick_audit.py"),
        ("tick_spot_check", "tick_audit.py"),
        ("estimate_gdp", "estimate_gdp.py"),
        ("event_load", "event_load.py"),
        ("ai_path_report", "ai_path_report.py"),
        ("review_branch", "review_branch.py"),
        ("fix_lint", "tools"),
    ],
)
def test_upstream_tool_looks_for_its_script_under_the_mod_root(
    shared_server, shared_fake_mod_root, name, missing
):
    error = _call(shared_server, name, TOOL_CALLS[name][0])["error"]

    assert shared_fake_mod_root.name in error
    assert missing in error


def test_check_equipment_variant_reads_the_mod_helper_and_hulls(server, fake_mod_root):
    validation = fake_mod_root / "tools" / "validation"
    (validation / "equipment_module_slots.py").write_text(
        """
class Finding:
    line = 4
    kind = "unknown_slot"
    message = "unused"
    hull = "test_hull"


from typing import NamedTuple


class CreatedVariants(NamedTuple):
    text: str
    spans: list[tuple[int, int]]


def build_equipment_index(units_dir):
    return object()


def created_variant_spans(content):
    return CreatedVariants(content, [])


def check_created_variants(variants, index):
    return []
""",
        encoding="utf-8",
    )
    equipment = fake_mod_root / "common" / "units" / "equipment"
    equipment.mkdir(parents=True)
    (equipment / "hulls.txt").write_text("test_hull = {}\n", encoding="utf-8")

    payload = _call(
        server,
        "check_equipment_variant",
        {"text": "create_equipment_variant = { type = test_hull modules = {} }"},
    )

    assert payload["ok"] is True
    assert payload["valid"] is True
    assert payload["issues"] == []


def test_generate_gfx_merge_reads_the_mod_textures_and_gfx_file(server, fake_mod_root):
    tex = fake_mod_root / "gfx" / "test"
    tex.mkdir(parents=True)
    for stem in ("one", "three"):
        (tex / f"{stem}.dds").write_bytes(b"x")

    payload = _call(
        server,
        "generate_gfx_merge",
        {
            "texture_dir": "gfx/test",
            "gfx_file": "interface/test_sprites.gfx",
            "prefix": "GFX_test_sprite_",
            "protected": ["GFX_test_tile"],
        },
    )

    assert payload["ok"] is True
    assert payload["new"] == ["GFX_test_sprite_three"]
    assert payload["orphaned"] == ["GFX_test_sprite_two"]
    assert 'name = "GFX_test_sprite_three"' in payload["txt"]
    assert payload["would_write"] is True


def test_fix_lint_runs_the_mod_fixers(fake_mod_root, cache_dir, server_factory):
    """fix_lint through FastMCP with upstream stand-ins planted in the mod root."""
    linting = fake_mod_root / "tools" / "linting"
    linting.mkdir(parents=True, exist_ok=True)
    (fake_mod_root / "tools" / "shared_utils.py").write_text(
        "def strip_inline_comment(line):\n    return line\n", encoding="utf-8"
    )
    (linting / "fix_styling.py").write_text(
        'def fix_line(line):\n    n = line.count("XX")\n    return line.replace("XX", "YY"), n\n',
        encoding="utf-8",
    )
    (linting / "fix_loc_yaml.py").write_text(
        "def check_line(line, line_num):\n    return []\ndef fix_line(line):\n    return line\n",
        encoding="utf-8",
    )
    (linting / "check_common_mistakes.py").write_text(
        "def _find_focus_log_mismatches(lines):\n    return []\n"
        "def _find_decision_log_mismatches(lines):\n    return []\n",
        encoding="utf-8",
    )
    srv = server_factory(in_process_settings(fake_mod_root, cache_dir))

    payload = _call(srv, "fix_lint", {"fixer": "styling", "content": "a XX b\n"})

    assert payload["ok"] is True
    assert payload["changed"] is True
    assert payload["txt"] == "a YY b\n"


def test_lint_and_validate_serialize_shared_runner(server, monkeypatch):
    """The two worker-thread handlers share a ValidatorRunner and must not race it."""
    state_lock = threading.Lock()
    active = 0
    peak_active = 0

    def track_runner_access(*args, **kwargs):
        nonlocal active, peak_active
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return {"ok": True}

    monkeypatch.setattr("md_mcp.server.validate_tool", track_runner_access)
    monkeypatch.setattr("md_mcp.server.lint_tool", track_runner_access)

    async def call_both():
        return await asyncio.gather(
            server.call_tool("validate", {}),
            server.call_tool("lint", {}),
        )

    results = asyncio.run(call_both())

    assert all(result and result[0].type == "text" for result in results)
    assert peak_active == 1


@pytest.mark.parametrize(
    ("tool", "implementation", "arguments"),
    [
        ("event_load", "event_load_tool", {}),
        ("ai_path_report", "ai_path_report_tool", {"tag": "USA"}),
    ],
)
def test_slow_tool_runs_off_the_event_loop(server, monkeypatch, tool, implementation, arguments):
    def slow_tool(*args, **kwargs):
        time.sleep(0.15)
        return {"ok": True}

    monkeypatch.setattr(f"md_mcp.server.{implementation}", slow_tool)

    async def go():
        task = asyncio.create_task(server.call_tool(tool, arguments))
        await asyncio.sleep(0.02)
        still_running = not task.done()
        await task
        return still_running

    assert asyncio.run(go()) is True


def test_event_load_forwards_its_defaults_in_order(server, monkeypatch):
    monkeypatch.setattr(
        "md_mcp.server.event_load_tool",
        lambda *args, **kwargs: {"ok": True, "window": args[2]},
    )

    assert _call(server, "event_load", {}) == {"ok": True, "window": 45}


def test_summarize_game_log_forwards_every_option(server, monkeypatch):
    observed: dict[str, Any] = {}

    def fake_summary(mod_root, path, **kwargs):
        observed.update({"mod_root": mod_root, "path": path, **kwargs})
        return {"ok": True, "path": path, "offset": kwargs["offset"]}

    monkeypatch.setattr("md_mcp.server.game_log_summary_tool", fake_summary)
    arguments = {
        "path": "/tmp/session.log",
        "top": 4,
        "limit": 2,
        "offset": 1,
        "countries": ["Brazil"],
        "since": "2001.1.1",
        "until": "2002.1.1",
    }

    payload = _call(server, "summarize_game_log", arguments)

    assert payload == {"ok": True, "path": "/tmp/session.log", "offset": 1}
    assert {key: observed[key] for key in arguments} == arguments


def test_definition_finder_ignores_client_kind_override(server):
    payload = _call(server, "find_characters", {"kind": "trait", "query": "test"})

    assert payload["total"] == 1
    assert payload["matches"][0]["kind"] == "character"


def test_resolve_sprite_falls_back_to_the_committed_manifest(
    fake_mod_root, cache_dir, server_factory
):
    """Full wiring: no HOI4 install + a committed manifest resolves a vanilla-only sprite."""
    (fake_mod_root / "tools" / "validation" / "vanilla_sprites.txt").write_text(
        "GFX_vanilla_only\n", encoding="utf-8"
    )
    srv = server_factory(in_process_settings(fake_mod_root, cache_dir))

    payload = _call(srv, "resolve_sprite", {"name": "GFX_vanilla_only"})

    assert payload["ok"] is True
    assert payload["source"] == "vanilla_manifest"


def test_review_branch_forwards_submod_root(fake_mod_root, cache_dir, tmp_path, server_factory):
    submod = tmp_path / "Overlay"
    submod.mkdir()
    srv = server_factory(_overlay_settings(fake_mod_root, cache_dir, submod))

    payload = _call(srv, "review_branch", {"base": "main"})

    # review_branch.py doesn't exist in the fake mod; the error must still shape
    # a valid payload, proving the tool wired submod_root through.
    assert payload["ok"] is False
    assert "review_branch.py not found" in payload["error"]


def test_focus_layout_tool_resolves_cross_file_shared_import(
    fake_mod_root, cache_dir, server_factory
):
    focus_dir = fake_mod_root / "common" / "national_focus"
    focus_dir.mkdir(parents=True, exist_ok=True)
    (focus_dir / "00_cross_file_shared.txt").write_text(
        "shared_focus = { id = TST_imported x = 3 y = 1 }\n",
        encoding="utf-8",
    )
    (focus_dir / "TST_cross_file_consumer.txt").write_text(
        """focus_tree = {
    id = TST_cross_file_tree
    shared_focus = TST_imported
    focus = { id = TST_consumer x = 1 y = 0 relative_position_id = TST_imported }
}
""",
        encoding="utf-8",
    )
    srv = server_factory(in_process_settings(fake_mod_root, cache_dir))

    result = _call(
        srv,
        "focus_layout",
        {"file": "common/national_focus/TST_cross_file_consumer.txt", "include_positions": True},
    )

    by_id = {position["id"]: position for position in result["positions"]}
    assert result["focus_count"] == 2
    assert by_id["TST_imported"]["file"] == "common/national_focus/00_cross_file_shared.txt"
    assert by_id["TST_consumer"]["x"] == 4


def test_analysis_tools_forward_submod_root(fake_mod_root, cache_dir, tmp_path, server_factory):
    submod_root = tmp_path / "overlay"
    focus = submod_root / "common/national_focus/OVR_overlay_only.txt"
    focus.parent.mkdir(parents=True)
    focus.write_text(
        """focus_tree = {
    focus = {
        id = OVR_overlay_only
        x = 15
        y = 2
        completion_reward = { country_event = OverlayMissing.1 }
    }
}
""",
        encoding="utf-8",
    )
    srv = server_factory(_overlay_settings(fake_mod_root, cache_dir, submod_root))

    layout = _call(srv, "focus_layout", {"tag": "OVR", "include_positions": True})
    refs = _call(srv, "check_refs", {"tag": "OVR", "kinds": ["event"]})

    assert layout["focus_count"] == 1
    assert layout["positions"] == [
        {
            "id": "OVR_overlay_only",
            "x": 15,
            "y": 2,
            "relative_to": None,
            "tree": "tree at line 1",
            "tree_line": 1,
            "file": "common/national_focus/OVR_overlay_only.txt",
            "line": 2,
            "dynamic": False,
        }
    ]
    assert refs["files_scanned"] == 1
    assert {entry["ref"] for entry in refs["unresolved"]} == {"OverlayMissing.1"}


def test_focus_overlap_runs_off_loop_and_forwards_scenario(server, monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()
    observed: dict[str, Any] = {}

    def slow_report(mod_root, file, tag, **kwargs):
        observed.update(mod_root=mod_root, file=file, tag=tag, **kwargs)
        started.set()
        assert release.wait(5)
        return {"ok": True, "file": file, "tag": tag}

    monkeypatch.setattr("md_mcp.server.focus_overlap_scenarios_tool", slow_report)

    async def exercise():
        slow = asyncio.create_task(
            server.call_tool(
                "focus_overlap_scenarios",
                {
                    "file": "common/national_focus/test.txt",
                    "tag": "TST",
                    "completed": ["TST_root"],
                    "date": "2001.1.1",
                    "include_positions": True,
                    "limit": 7,
                    "offset": 2,
                },
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 5)
            fast = await asyncio.wait_for(
                server.call_tool("calculate_days", {"year": 2000, "month": 1, "day": 1}),
                timeout=2,
            )
            assert json.loads(fast[0].text) == {"ok": True, "days": 0}
            assert not slow.done()
        finally:
            release.set()
        result = await slow
        assert json.loads(result[0].text) == {
            "ok": True,
            "file": "common/national_focus/test.txt",
            "tag": "TST",
        }

    asyncio.run(exercise())
    assert observed["tag"] == "TST"
    assert observed["completed"] == ["TST_root"]
    assert observed["date"] == "2001.1.1"
    assert observed["limit"] == 7 and observed["offset"] == 2
