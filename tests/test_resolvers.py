"""Resolver regression tests — focus anchoring against the index record (issue #153).

The focus index resolves duplicate ids last-write-wins, so `resolve_focus_tool` must
re-anchor freshly parsed records to the line the index picked instead of taking the
first match in the file. Anchoring rules come from `util/index_anchor.py`, shared
with the `md://` resources.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from md_mcp.analysis.vanilla_manifest import load_sprite_manifest
from md_mcp.config import Settings
from md_mcp.indexes import (
    CharacterIndex,
    CountryTagIndex,
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
    ScriptedEffectIndex,
    ScriptedTriggerIndex,
    TraitIndex,
)
from md_mcp.resources import focus_resource
from md_mcp.tools.resolver_tools import (
    resolve_character_tool,
    resolve_country_tag_tool,
    resolve_decision_tool,
    resolve_event_tool,
    resolve_focus_tool,
    resolve_idea_tool,
    resolve_loc_tool,
    resolve_scripted_effect_tool,
    resolve_scripted_trigger_tool,
    resolve_sprite_tool,
    resolve_trait_tool,
)
from md_mcp.util.index_anchor import IndexAnchorError
from tests.support.mod import in_process_settings, mod_settings


def _focus_mod(tmp_path: Path, *texts: str) -> tuple[Settings, FocusIndex]:
    """Write one `common/national_focus/<n>.txt` per given text and build the index."""
    mod_root = tmp_path / "Mod"
    focus_dir = mod_root / "common" / "national_focus"
    focus_dir.mkdir(parents=True)
    for i, text in enumerate(texts):
        (focus_dir / f"focus_{i}.txt").write_text(text, encoding="utf-8")
    settings = mod_settings(mod_root, tmp_path / ".md-mcp-cache")
    return settings, FocusIndex(mod_root, settings.cache_dir)


def _duplicate_tree(second_x: int = 9) -> str:
    return (
        "focus_tree = {\n"
        "\tfocus = {\n"
        "\t\tid = TST_dup\n"
        "\t\tx = 1\n"
        "\t}\n"
        "\tfocus = {\n"
        "\t\tid = TST_dup\n"
        f"\t\tx = {second_x}\n"
        "\t}\n"
        "}\n"
    )


class _FrozenFocusIndex:
    """Duck-typed FocusIndex whose `resolve` returns a fixed record unchanged."""

    def __init__(self, rec: dict):
        self._rec = rec

    def resolve(self, key: str) -> dict:
        return self._rec


def test_resolve_focus_returns_parsed_details_for_unique_focus(tmp_path):
    settings, index = _focus_mod(
        tmp_path,
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_a\n\t\tx = 4\n\t\ty = 5\n\t}\n}\n",
    )

    result = resolve_focus_tool("TST_a", settings, index)

    assert result["ok"] is True
    assert Path(result["file"]) == Path("common/national_focus/focus_0.txt")
    assert result["kind"] == "focus_tree"
    assert result["line"] == 2
    assert result["parsed"]["x"] == 4
    assert result["parsed"]["y"] == 5


@pytest.mark.parametrize("shared", [False, True])
def test_resolve_focus_anchors_duplicate_to_the_indexed_later_record(tmp_path, shared):
    container = ("shared_focus", "}\n") if shared else ("focus_tree", "}\n")
    text = (
        container[0]
        + " {\n"
        + "\tfocus = {\n\t\tid = TST_dup\n\t\tx = 1\n\t}\n"
        + "\tfocus = {\n\t\tid = TST_dup\n\t\tx = 9\n\t}\n"
        + container[1]
    )
    if shared:
        # shared_focus holds the definitions directly; drop the inner `focus =`.
        text = (
            "shared_focus = {\n"
            "\tid = TST_dup\n"
            "\tx = 1\n"
            "}\n"
            "shared_focus = {\n"
            "\tid = TST_dup\n"
            "\tx = 9\n"
            "}\n"
        )
    settings, index = _focus_mod(tmp_path, text)
    rec = index.resolve("TST_dup")
    assert rec is not None
    # The index keys the last occurrence in file order.
    assert rec["line"] == (5 if shared else 6)

    result = resolve_focus_tool("TST_dup", settings, index)

    assert result["ok"] is True
    assert result["line"] == (5 if shared else 6)
    assert result["parsed"]["x"] == 9


def test_resolve_focus_anchors_duplicate_across_files_to_the_indexed_winner(tmp_path):
    # Canonical relpath order makes focus_1.txt the last-write-wins contributor.
    settings, index = _focus_mod(
        tmp_path,
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_dup\n\t\tx = 1\n\t}\n}\n",
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_dup\n\t\tx = 9\n\t}\n}\n",
    )
    rec = index.resolve("TST_dup")
    assert rec is not None
    assert Path(rec["file"]) == Path("common/national_focus/focus_1.txt")

    result = resolve_focus_tool("TST_dup", settings, index)

    assert result["ok"] is True
    assert Path(result["file"]) == Path("common/national_focus/focus_1.txt")
    assert result["parsed"]["x"] == 9


def test_resolve_focus_walks_tree_shared_and_joint_candidates(tmp_path):
    settings, index = _focus_mod(
        tmp_path,
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_tree\n\t\tx = 1\n\t}\n}\n"
        "shared_focus = {\n\tid = TST_shared\n\tx = 2\n}\n"
        "joint_focus = {\n\tid = TST_joint\n\tx = 3\n}\n",
    )

    tree = resolve_focus_tool("TST_tree", settings, index)
    shared = resolve_focus_tool("TST_shared", settings, index)
    joint = resolve_focus_tool("TST_joint", settings, index)

    assert (tree["kind"], tree["line"]) == ("focus_tree", 2)
    assert (shared["kind"], shared["line"]) == ("shared_focus", 7)
    assert (joint["kind"], joint["line"]) == ("joint_focus", 11)
    assert [tree["parsed"]["x"], shared["parsed"]["x"], joint["parsed"]["x"]] == [1, 2, 3]


def test_resolve_focus_on_cross_kind_duplicate_anchors_indexed_record(tmp_path):
    # A tree focus and a joint_focus sharing an id: the top-level joint_focus is
    # the file-later definition and therefore the index winner.
    settings, index = _focus_mod(
        tmp_path,
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_dup\n\t\tx = 1\n\t}\n}\n"
        "joint_focus = {\n\tid = TST_dup\n\tx = 3\n}\n",
    )
    rec = index.resolve("TST_dup")
    assert rec is not None
    assert rec["kind"] == "joint_focus"

    result = resolve_focus_tool("TST_dup", settings, index)

    assert result["ok"] is True
    assert result["kind"] == "joint_focus"
    assert result["parsed"]["x"] == 3


def test_resolve_focus_stale_indexed_line_returns_clear_error(tmp_path):
    text = "focus_tree = {\n\tfocus = {\n\t\tid = TST_moved\n\t}\n}\n"
    settings, index = _focus_mod(tmp_path, text)
    rec = index.resolve("TST_moved")
    assert rec is not None
    # The focus moved after indexing: simulate the index still pointing at the
    # focus_tree container line, where no definition starts.
    frozen = _FrozenFocusIndex({**rec, "line": 1})

    result = resolve_focus_tool("TST_moved", settings, cast(FocusIndex, frozen))

    assert result["ok"] is False
    assert "stale" in result["error"]
    assert "build-index" in result["error"]
    assert result["line"] == 1
    assert result["kind"] == rec["kind"]


def test_resolve_focus_disappeared_id_returns_clear_error(tmp_path):
    settings, index = _focus_mod(
        tmp_path, "focus_tree = {\n\tfocus = {\n\t\tid = TST_gone\n\t}\n}\n"
    )
    rec = index.resolve("TST_gone")
    assert rec is not None
    # The id no longer exists in the file at all, but the stale index still
    # resolves it: simulate by freezing the record and rewriting the file.
    rewritten = (settings.mod_root / rec["file"]).resolve()
    rewritten.write_text(
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_other\n\t}\n}\n", encoding="utf-8"
    )
    frozen = _FrozenFocusIndex(rec)

    result = resolve_focus_tool("TST_gone", settings, cast(FocusIndex, frozen))

    assert result["ok"] is False
    assert "stale" in result["error"]
    assert "build-index" in result["error"]


def test_resolve_focus_same_line_duplicates_return_clear_ambiguity_error(tmp_path):
    settings, index = _focus_mod(
        tmp_path, "focus_tree = { focus = { id = TST_d } focus = { id = TST_d } }\n"
    )
    rec = index.resolve("TST_d")
    assert rec is not None
    assert rec["line"] == 1

    result = resolve_focus_tool("TST_d", settings, index)

    assert result["ok"] is False
    assert "ambiguous" in result["error"]
    assert "line 1" in result["error"]


def test_resolve_focus_several_candidates_without_indexed_line_is_ambiguous(tmp_path):
    settings, index = _focus_mod(tmp_path, _duplicate_tree())
    rec = index.resolve("TST_dup")
    assert rec is not None
    frozen = _FrozenFocusIndex({**rec, "line": None})

    result = resolve_focus_tool("TST_dup", settings, cast(FocusIndex, frozen))

    assert result["ok"] is False
    assert "ambiguous" in result["error"]
    assert "no line to disambiguate" in result["error"]


def test_resolve_focus_lone_candidate_without_indexed_line_still_resolves(tmp_path):
    settings, index = _focus_mod(
        tmp_path, "focus_tree = {\n\tfocus = {\n\t\tid = TST_a\n\t\tx = 2\n\t}\n}\n"
    )
    rec = index.resolve("TST_a")
    assert rec is not None
    frozen = _FrozenFocusIndex({**rec, "line": None})

    result = resolve_focus_tool("TST_a", settings, cast(FocusIndex, frozen))

    assert result["ok"] is True
    assert result["parsed"]["x"] == 2


def test_index_anchor_error_is_a_keyerror():
    # Resources raise KeyError through the MCP layer; the shared error keeps that
    # contract.
    assert issubclass(IndexAnchorError, KeyError)


def test_resolve_focus_reads_overlay_and_reports_overlay_line(fake_mod_root, tmp_path):
    submod = tmp_path / "Overlay"
    focus_dir = submod / "common" / "national_focus"
    focus_dir.mkdir(parents=True)
    (focus_dir / "test.txt").write_text(
        "focus_tree = {\n\n\n    focus = { id = TST_root }\n}\n",
        encoding="utf-8",
    )
    settings = Settings(
        mod_root=fake_mod_root,
        vanilla_path=None,
        cache_dir=tmp_path / "cache",
    )
    vars(settings)["submod_root"] = submod
    index = FocusIndex(fake_mod_root, settings.cache_dir)
    vars(index)["submod_root"] = submod

    result = resolve_focus_tool("TST_root", settings, index)

    assert result["ok"] is True
    assert result["file"] == "common/national_focus/test.txt"
    assert result["line"] == 4


def test_resolve_focus_falls_back_to_the_indexed_record(fake_mod_root, cache_dir):
    settings = in_process_settings(fake_mod_root, cache_dir)
    index = FocusIndex(fake_mod_root, cache_dir)
    record = index.resolve("TST_root")
    assert record is not None
    indexed = {
        "ok": True,
        "id": "TST_root",
        "file": record["file"],
        "line": record["line"],
        "kind": record["kind"],
    }
    source = fake_mod_root / record["file"]

    # A second freshness check arms the debounce, so the index keeps the record
    # while the file changes under it.
    index.ensure_fresh()
    source.write_text("focus_tree = { focus = { id = TST_root x = {{{", encoding="utf-8")
    unparsable = resolve_focus_tool("TST_root", settings, index)
    assert unparsable.pop("warning").startswith("Could not parse file for detail extraction")
    assert unparsable == indexed

    source.unlink()
    missing = resolve_focus_tool("TST_root", settings, index)
    assert missing == {**indexed, "warning": "File listed in index but no longer on disk"}


def test_resolve_focus_and_loc_report_a_missing_key(fake_mod_root, cache_dir):
    settings = mod_settings(fake_mod_root, cache_dir)
    focuses = FocusIndex(fake_mod_root, cache_dir)
    loc = LocalisationIndex(fake_mod_root, cache_dir, include_vanilla=False)

    assert resolve_focus_tool("TST_absent", settings, focuses) == {
        "ok": False,
        "id": "TST_absent",
        "error": "Focus not found in mod or vanilla",
    }
    assert resolve_loc_tool("TST_absent", settings, loc) == {
        "ok": False,
        "key": "TST_absent",
        "lang": "en",
        "error": "Loc key not found",
    }


def test_resolve_loc_returns_the_value_and_where_it_is(fake_mod_root, cache_dir):
    settings = mod_settings(fake_mod_root, cache_dir)
    loc = LocalisationIndex(fake_mod_root, cache_dir, include_vanilla=False)

    found = resolve_loc_tool("TST_root", settings, loc)

    assert Path(found.pop("file")) == Path("localisation/english/test_l_english.yml")
    assert found == {
        "ok": True,
        "key": "TST_root",
        "lang": "en",
        "value": "The Root Focus",
        "line": 2,
    }


# (index, resolver, an id in the fake mod, fields its record carries, the miss message)
RESOLVERS = [
    (
        EventIndex,
        resolve_event_tool,
        "Testns.1",
        {
            "kind": "country_event",
            "namespace": "Testns",
            "file_namespaces": ["Testns", "TestnsNews"],
        },
        "Event not found",
    ),
    (
        DecisionIndex,
        resolve_decision_tool,
        "TST_simple_decision",
        {"category": "TST_category"},
        "Decision not found",
    ),
    (
        IdeaIndex,
        resolve_idea_tool,
        "TST_acme_tanks",
        {"category": "tank_manufacturer", "slot": None},
        "Idea not found",
    ),
    (
        CountryTagIndex,
        resolve_country_tag_tool,
        "TST",
        {
            "kind": "country_tag",
            "name": "TST",
            "tag": "TST",
            "country_file": "countries/Testland.txt",
        },
        "Country tag not found",
    ),
    (
        CharacterIndex,
        resolve_character_tool,
        "TST_test_character",
        {"kind": "character", "name": "TST_test_character"},
        "Character not found",
    ),
    (TraitIndex, resolve_trait_tool, "TST_test_trait", {"kind": "trait"}, "Trait not found"),
    (
        ScriptedEffectIndex,
        resolve_scripted_effect_tool,
        "TST_test_effect",
        {"kind": "scripted_effect"},
        "Scripted effect not found",
    ),
    (
        ScriptedTriggerIndex,
        resolve_scripted_trigger_tool,
        "TST_test_trigger",
        {"kind": "scripted_trigger"},
        "Scripted trigger not found",
    ),
]


@pytest.mark.parametrize(("index_cls", "resolve", "ident", "fields", "miss"), RESOLVERS)
def test_resolver_returns_the_record_or_a_shaped_miss(
    fake_mod_root, cache_dir, index_cls, resolve, ident, fields, miss
):
    index = index_cls(fake_mod_root, cache_dir, include_vanilla=False)

    found = resolve(ident, index)

    assert found["ok"] is True
    assert found["id"] == ident
    assert found["line"] >= 1
    assert {name: found[name] for name in fields} == fields
    assert resolve("TST_absent", index) == {"ok": False, "id": "TST_absent", "error": miss}


@pytest.mark.parametrize(
    ("name", "manifest", "expected"),
    [
        pytest.param(
            "GFX_test_sprite_one",
            {"GFX_test_sprite_one"},
            {"ok": True, "source": None, "texturefile": "gfx/test/one.dds", "line": 2},
            id="index-wins-over-manifest",
        ),
        pytest.param(
            "GFX_vanilla_icon",
            {"GFX_vanilla_icon", "GFX_test_sprite_one"},
            {"ok": True, "source": "vanilla_manifest", "file": None},
            id="manifest-fallback",
        ),
        pytest.param("GFX_nope", {"GFX_other"}, {"ok": False}, id="in-neither"),
        pytest.param("GFX_vanilla_icon", None, {"ok": False}, id="no-manifest-no-fallback"),
    ],
)
def test_resolve_sprite_prefers_the_index_then_the_manifest(
    fake_mod_root, cache_dir, name, manifest, expected
):
    """Vanilla-manifest fallback for sprites the mod does not define (issue #23)."""
    index = GfxIndex(fake_mod_root, cache_dir, None)
    vanilla_sprites = None if manifest is None else frozenset(manifest)

    out = resolve_sprite_tool(name, index, vanilla_sprites)

    assert {key: out.get(key) for key in expected} == expected


@pytest.mark.integration
def test_manifest_fallback_against_real_mod(real_mod_root, tmp_path) -> None:
    """A vanilla-only sprite resolves from the real committed manifest (no install)."""
    cache = tmp_path / ".md-mcp-cache"
    idx = GfxIndex(real_mod_root, cache, None)
    manifest = load_sprite_manifest(real_mod_root)
    assert manifest is not None
    out = resolve_sprite_tool("GFX_3d_model_bg_air", idx, manifest)
    assert out["ok"] is True
    assert out["source"] == "vanilla_manifest"


@pytest.mark.integration
def test_resolve_focus_and_resource_smoke_on_real_mod(real_mod_root, tmp_path):
    """Smoke: on the real mod every focus id resolves ok and resources slice it."""
    settings = mod_settings(real_mod_root, tmp_path / ".md-mcp-cache")
    index = FocusIndex(real_mod_root, settings.cache_dir)

    ids = [key for key in index.list_keys()[:3]]
    assert ids, "no focus ids indexed at all"

    for focus_id in ids:
        result = resolve_focus_tool(focus_id, settings, index)
        assert result["ok"] is True, (focus_id, result)
        assert result["parsed"] is not None

        resource = focus_resource(focus_id, settings, index)
        assert f"id = {focus_id}" in resource
        assert resource.lstrip().startswith(("focus", "shared_focus", "joint_focus"))
