"""Generator tests — every emitted block must round-trip through the parser."""

from __future__ import annotations

import inspect
import json

import pytest

from md_mcp.generators import (
    generate_decision,
    generate_event,
    generate_focus,
    generate_gfx_entry,
    generate_gfx_merge,
    generate_idea,
    generate_loc_stub,
)
from md_mcp.generators import gfx as gfx_mod
from md_mcp.paradox import parse_string
from md_mcp.paradox.schema import (
    extract_decision_records,
    extract_event_records,
    extract_focus_records,
    extract_idea_records,
    extract_sprite_records,
)
from md_mcp.util.response import BUDGET_BYTES


def test_focus_round_trips_and_extracts_correctly():
    r = generate_focus(
        id="TST_root",
        tag="TST",
        x=0,
        y=0,
        cost=10,
        completion_reward="add_political_power = 50",
    )
    # Wrap in focus_tree so the schema extractor sees it the way it would in-game.
    wrapped = "focus_tree = { id = test\n" + r["txt"] + "\n}"
    root = parse_string(wrapped)
    records = extract_focus_records(root, source=wrapped)
    assert any(rec["id"] == "TST_root" for rec in records)
    keys = {entry["key"] for entry in r["loc_yml_keys"]}
    assert keys == {"TST_root", "TST_root_desc"}


def test_focus_emits_prereq_and_mutex_correctly():
    r = generate_focus(
        id="TST_branch",
        tag="TST",
        x=1,
        y=1,
        relative_position_id="TST_root",
        prerequisites=[["TST_root"]],
        mutually_exclusive=["TST_other"],
    )
    wrapped = "focus_tree = { id = test\n" + r["txt"] + "\n}"
    root = parse_string(wrapped)
    [rec] = extract_focus_records(root)
    assert rec["prerequisites"] == [["TST_root"]]
    assert rec["mutually_exclusive"] == ["TST_other"]
    assert rec["relative_position_id"] == "TST_root"


def test_focus_includes_log_line():
    r = generate_focus(id="TST_x", tag="TST", x=0, y=0)
    assert 'log = "[GetDateText]: [Root.GetName]: Focus TST_x"' in r["txt"]


def test_event_round_trips_with_options():
    r = generate_event(
        namespace="Test",
        number=1,
        options=[
            {"label": "Yes", "effects": "add_political_power = 25"},
            {"label": "No"},
        ],
    )
    root = parse_string(r["txt"])
    [rec] = extract_event_records(root)
    assert rec["id"] == "Test.1"
    assert rec["kind"] == "country_event"

    keys = {entry["key"] for entry in r["loc_yml_keys"]}
    assert keys == {"Test.1.t", "Test.1.d", "Test.1.a", "Test.1.b"}
    assert r["namespace_directive"] == "add_namespace = Test"


def test_event_invalid_kind_rejected():
    with pytest.raises(ValueError):
        generate_event(namespace="X", number=1, kind="bogus_event")


def test_event_too_many_options_rejected():
    with pytest.raises(ValueError):
        generate_event(namespace="X", number=1, options=[{}] * 27)


def test_event_invalid_ai_chance_rejected():
    with pytest.raises(ValueError, match="ai_chance must be an integer"):
        generate_event(namespace="X", number=1, options=[{"ai_chance": "bad"}])


def test_simple_generators_bound_oversized_payloads():
    huge = "x" * 120_000
    results = [
        generate_focus(id="TST_x", tag="TST", x=0, y=0, completion_reward=huge),
        generate_event(namespace="TST", number=1, options=[{"effects": huge}]),
        generate_decision(id="TST_dec", complete_effect=huge),
        generate_idea(id="TST_idea", modifier=huge),
        generate_gfx_entry(name=huge, texturefile="gfx/test.dds"),
        generate_loc_stub([{"key": "K", "value": huge}]),
    ]

    for result in results:
        assert result["size_truncated"] is True
        assert result["txt_dropped"] > 0
        assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_decision_round_trips():
    r = generate_decision(id="TST_dec", tag="TST", cost=50, complete_effect="add_stability = 0.05")
    # Wrap in a category since the extractor requires that nesting.
    wrapped = "TST_cat = {\n" + r["txt"] + "\n}"
    root = parse_string(wrapped)
    records = extract_decision_records(root)
    assert [rec["id"] for rec in records] == ["TST_dec"]
    assert records[0]["category"] == "TST_cat"
    # `allowed = { original_tag = TAG }` is auto-built from the tag arg.
    assert "original_tag = TST" in r["txt"]


def test_idea_round_trips():
    r = generate_idea(id="TST_idea", tag="TST", modifier="stability_factor = 0.05")
    wrapped = "ideas = { country = {\n" + r["txt"] + "\n} }"
    root = parse_string(wrapped)
    records = extract_idea_records(root)
    assert [rec["id"] for rec in records] == ["TST_idea"]
    assert "original_tag = TST" in r["txt"]


def test_gfx_round_trips():
    r = generate_gfx_entry(name="GFX_test", texturefile="gfx/test.dds", frames=4)
    wrapped = "spriteTypes = {\n" + r["txt"] + "\n}"
    root = parse_string(wrapped)
    [rec] = extract_sprite_records(root)
    assert rec["name"] == "GFX_test"
    assert rec["texturefile"] == "gfx/test.dds"


def test_loc_stub_bom_and_escaping():
    r = generate_loc_stub(
        [
            {"key": "K1", "value": "Simple"},
            {"key": "K2", "value": 'has "quotes" and a \\ backslash'},
        ],
        bom_prefix=True,
    )
    assert r["txt"].startswith("﻿")
    assert "l_english:" in r["txt"]
    assert r"\"" in r["txt"]
    assert r["bytes_to_write"].startswith(b"\xef\xbb\xbf")


def test_loc_stub_append_mode_skips_header():
    r = generate_loc_stub([{"key": "K", "value": "V"}], include_header=False)
    assert "l_english:" not in r["txt"]
    assert ' K: "V"' in r["txt"]


def _render(name: str, tex: str) -> str:
    return f'\tspriteType = {{\n\t\tname = "{name}"\n\t\ttexturefile = "{tex}"\n\t}}\n'


def test_merge_appends_new_and_keeps_unchanged():
    original = "spriteTypes = {\n" + _render("GFX_a", "gfx/a.dds") + "}\n"
    out = gfx_mod.merge_gfx_text(original, {"GFX_a": "gfx/a.dds", "GFX_b": "gfx/b.dds"}, _render)
    assert out["new"] == ["GFX_b"]
    assert out["changed"] == []
    assert out["orphaned"] == []
    assert out["would_write"] is True
    assert 'name = "GFX_a"' in out["txt"]
    assert 'name = "GFX_b"' in out["txt"]
    assert original.split("GFX_a")[0] in out["txt"]


def test_merge_replaces_changed_texture_in_place():
    original = "spriteTypes = {\n" + _render("GFX_a", "gfx/old.dds") + "}\n"
    out = gfx_mod.merge_gfx_text(original, {"GFX_a": "gfx/new.dds"}, _render)
    assert out["changed"] == [("GFX_a", "gfx/old.dds")]
    assert out["new"] == []
    assert "gfx/new.dds" in out["txt"]
    assert "gfx/old.dds" not in out["txt"]
    assert "\tspriteType" in out["txt"]


def test_merge_reports_orphans_and_does_not_delete_them():
    original = (
        "spriteTypes = {\n"
        + _render("GFX_keep", "gfx/keep.dds")
        + _render("GFX_gone", "gfx/gone.dds")
        + "}\n"
    )
    out = gfx_mod.merge_gfx_text(original, {"GFX_keep": "gfx/keep.dds"}, _render)
    assert out["orphaned"] == ["GFX_gone"]
    assert 'name = "GFX_gone"' in out["txt"]
    assert out["would_write"] is False


def test_merge_dedup_same_texture():
    original = (
        "spriteTypes = {\n"
        + _render("GFX_a", "gfx/a.dds")
        + _render("GFX_a", "gfx/a.dds")
        + _render("GFX_b", "gfx/b.dds")
        + "}\n"
    )
    out = gfx_mod.merge_gfx_text(original, {"GFX_a": "gfx/a.dds", "GFX_b": "gfx/b.dds"}, _render)
    assert out["deduped"] == ["GFX_a"]
    assert out["conflicts"] == []
    assert out["txt"].count('name = "GFX_a"') == 1
    assert out["would_write"] is True


def test_merge_dedup_divergent_texture_is_reported():
    original = (
        "spriteTypes = {\n" + _render("GFX_a", "gfx/a.dds") + _render("GFX_a", "gfx/a2.dds") + "}\n"
    )
    out = gfx_mod.merge_gfx_text(original, {"GFX_a": "gfx/a.dds"}, _render)
    assert out["deduped"] == ["GFX_a"]
    assert out["conflicts"] == [{"name": "GFX_a", "kept": "gfx/a.dds", "dropped": "gfx/a2.dds"}]
    assert out["txt"].count('name = "GFX_a"') == 1
    assert "gfx/a2.dds" not in out["txt"]


def test_merge_dedup_removes_trailing_inline_comment():
    dup = (
        "\tspriteType = {\n"
        '\t\tname = "GFX_a"\n'
        '\t\ttexturefile = "gfx/a.dds"\n'
        "\t} # duplicate, remove me\n"
    )
    original = "spriteTypes = {\n" + _render("GFX_a", "gfx/a.dds") + dup + "}\n"
    out = gfx_mod.merge_gfx_text(original, {"GFX_a": "gfx/a.dds"}, _render)
    assert "remove me" not in out["txt"]
    assert out["txt"].count('name = "GFX_a"') == 1


def test_merge_dedup_of_last_block_keeps_closing_brace():
    original = (
        "spriteTypes = {\n"
        + _render("GFX_b", "gfx/b.dds")
        + _render("GFX_a", "gfx/a.dds")
        + _render("GFX_a", "gfx/a.dds")
        + "}\n"
    )
    out = gfx_mod.merge_gfx_text(original, {"GFX_a": "gfx/a.dds", "GFX_b": "gfx/b.dds"}, _render)
    assert out["txt"].count('name = "GFX_a"') == 1
    assert out["txt"].rstrip().endswith("}")


def test_merge_protected_name_is_not_updated():
    original = "spriteTypes = {\n" + _render("GFX_keep", "gfx/old.dds") + "}\n"
    out = gfx_mod.merge_gfx_text(
        original,
        {"GFX_keep": "gfx/new.dds"},
        _render,
        protected=frozenset({"GFX_keep"}),
    )
    assert out["changed"] == []
    assert "gfx/old.dds" in out["txt"]
    assert out["would_write"] is False


def test_merge_is_idempotent():
    original = (
        "spriteTypes = {\n" + _render("GFX_a", "gfx/a.dds") + _render("GFX_a", "gfx/a.dds") + "}\n"
    )
    entries = {"GFX_a": "gfx/a.dds"}
    first = gfx_mod.merge_gfx_text(original, entries, _render)
    second = gfx_mod.merge_gfx_text(first["txt"], entries, _render)
    assert second["deduped"] == []
    assert second["would_write"] is False
    assert second["txt"] == first["txt"]


def test_subprocess_generator_is_gone():
    assert not hasattr(gfx_mod, "subprocess_generator")


def test_generate_gfx_merge_signature():
    params = inspect.signature(generate_gfx_merge).parameters
    for p in ("texture_dir", "gfx_file", "prefix", "limit", "offset", "include_file"):
        assert p in params


def _plant_merge_mod(root, *, textures, gfx_body=None):
    tex = root / "gfx" / "icons"
    tex.mkdir(parents=True)
    for name in textures:
        (tex / f"{name}.dds").write_bytes(b"x")
    gfx_path = root / "interface" / "icons.gfx"
    gfx_path.parent.mkdir(parents=True, exist_ok=True)
    if gfx_body is not None:
        gfx_path.write_text(gfx_body, encoding="utf-8")
    return tex, gfx_path


def test_generate_gfx_merge_appends_without_writing(tmp_path):
    existing = "spriteTypes = {\n" + _render("GFX_old", "gfx/icons/old.dds") + "}\n"
    _tex, gfx_path = _plant_merge_mod(tmp_path, textures=["old", "new"], gfx_body=existing)
    before = gfx_path.read_text(encoding="utf-8")
    r = generate_gfx_merge(
        tmp_path, texture_dir="gfx/icons", gfx_file="interface/icons.gfx", prefix="GFX_"
    )
    assert r["ok"] is True
    assert r["exists"] is True
    assert r["new"] == ["GFX_new"]
    assert r["orphaned"] == []
    assert "GFX_new" in r["txt"]
    assert r["txt"].startswith("\tspriteType")
    assert r["would_write"] is True
    assert gfx_path.read_text(encoding="utf-8") == before


def test_generate_gfx_merge_new_file_returns_full_document(tmp_path):
    _plant_merge_mod(tmp_path, textures=["only"])
    r = generate_gfx_merge(
        tmp_path, texture_dir="gfx/icons", gfx_file="interface/icons.gfx", prefix="GFX_"
    )
    assert r["ok"] is True
    assert r["exists"] is False
    assert r["txt"].startswith("spriteTypes = {")
    assert r["txt"].rstrip().endswith("}")
    assert 'name = "GFX_only"' in r["txt"]
    assert not (tmp_path / "interface" / "icons.gfx").exists()


def test_generate_gfx_merge_skips_existing_prefix_on_stem(tmp_path):
    _plant_merge_mod(tmp_path, textures=["GFX_already"])
    r = generate_gfx_merge(
        tmp_path, texture_dir="gfx/icons", gfx_file="interface/icons.gfx", prefix="GFX_"
    )
    assert r["new"] == ["GFX_already"]
    assert "GFX_GFX_already" not in r["txt"]


def test_generate_gfx_merge_limit_truncates(tmp_path):
    _plant_merge_mod(tmp_path, textures=["a", "b", "c"])
    r = generate_gfx_merge(
        tmp_path, texture_dir="gfx/icons", gfx_file="interface/icons.gfx", prefix="GFX_", limit=1
    )
    assert r["ok"] is True
    assert r["new_total"] == 3
    assert len(r["new"]) == 1
    assert r["truncated"] is True


def test_generate_gfx_merge_include_file_drops_when_over_budget(tmp_path):
    pad = "# " + ("x" * 120_000) + "\n"
    existing = "spriteTypes = {\n" + pad + "}\n"
    _plant_merge_mod(tmp_path, textures=["a"], gfx_body=existing)
    r = generate_gfx_merge(
        tmp_path,
        texture_dir="gfx/icons",
        gfx_file="interface/icons.gfx",
        prefix="GFX_",
        include_file=True,
    )
    assert r["ok"] is True
    assert r.get("size_truncated") is True
    assert "file_txt" not in r
    assert r["file_txt_dropped"] >= 1


def test_generate_gfx_merge_txt_is_bounded_by_limit(tmp_path):
    _plant_merge_mod(tmp_path, textures=[f"icon_{i:03d}" for i in range(400)])
    r = generate_gfx_merge(
        tmp_path, texture_dir="gfx/icons", gfx_file="interface/icons.gfx", prefix="GFX_", limit=100
    )
    assert r["new_total"] == 400
    assert "txt" in r
    assert r["txt"].count("spriteType = {") == 100
    assert r.get("size_truncated") is None


def test_generate_gfx_merge_txt_pages_rebuild_the_document(tmp_path):
    _plant_merge_mod(tmp_path, textures=["a", "b", "c"])
    first = generate_gfx_merge(
        tmp_path,
        texture_dir="gfx/icons",
        gfx_file="interface/icons.gfx",
        prefix="GFX_",
        limit=2,
        offset=0,
        include_file=True,
    )
    second = generate_gfx_merge(
        tmp_path,
        texture_dir="gfx/icons",
        gfx_file="interface/icons.gfx",
        prefix="GFX_",
        limit=2,
        offset=2,
    )
    assert first["txt"].startswith("spriteTypes = {")
    assert not first["txt"].endswith("}\n}\n")
    assert not second["txt"].startswith("spriteTypes = {")
    assert second["txt"].endswith("}\n}\n")
    assert first["txt"] + second["txt"] == first["file_txt"]


def test_generate_gfx_merge_empty_file_returns_full_document(tmp_path):
    _plant_merge_mod(tmp_path, textures=["only"], gfx_body="")
    r = generate_gfx_merge(
        tmp_path, texture_dir="gfx/icons", gfx_file="interface/icons.gfx", prefix="GFX_"
    )
    assert r["exists"] is True
    assert r["txt"].startswith("spriteTypes = {")
    assert r["txt"].rstrip().endswith("}")


def test_generate_gfx_merge_paginates_scan_duplicates(tmp_path):
    tex = tmp_path / "gfx" / "icons"
    for sub in ("a", "b"):
        (tex / sub).mkdir(parents=True)
        for i in range(5):
            (tex / sub / f"dup_{i}.dds").write_bytes(b"x")
    r = generate_gfx_merge(
        tmp_path, texture_dir="gfx/icons", gfx_file="interface/icons.gfx", prefix="GFX_", limit=2
    )
    assert r["scanned"] == 10
    assert r["scan_duplicate_total"] == 5
    assert len(r["scan_duplicates"]) == 2
    assert r["truncated"] is True


def test_generate_gfx_merge_requires_prefix():
    params = inspect.signature(generate_gfx_merge).parameters
    assert params["prefix"].default is inspect.Parameter.empty


def test_generate_gfx_merge_missing_dir(tmp_path):
    r = generate_gfx_merge(
        tmp_path, texture_dir="gfx/missing", gfx_file="interface/icons.gfx", prefix="GFX_"
    )
    assert r["ok"] is False
    assert "not a directory" in r["error"]
    assert r["texture_dir"] == "gfx/missing"


def test_generate_gfx_merge_rejects_path_escape(tmp_path):
    r = generate_gfx_merge(
        tmp_path, texture_dir="../outside", gfx_file="interface/icons.gfx", prefix="GFX_"
    )
    assert r["ok"] is False
    assert "escapes mod root" in r["error"]


def test_generate_gfx_merge_rejects_gfx_file_escape(tmp_path):
    tex = tmp_path / "gfx"
    tex.mkdir()
    (tex / "a.dds").write_bytes(b"x")
    r = generate_gfx_merge(tmp_path, texture_dir="gfx", gfx_file="../outside.gfx", prefix="GFX_")
    assert r["ok"] is False
    assert "gfx_file escapes mod root" in r["error"]
