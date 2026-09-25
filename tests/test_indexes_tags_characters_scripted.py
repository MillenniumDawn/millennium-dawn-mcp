"""Indexes for tags, characters/traits, and scripted definitions."""

from __future__ import annotations

import pytest

from md_mcp.analysis.manifest import list_country_content
from md_mcp.analysis.ref_audit import check_refs
from md_mcp.analysis.refs import Kind, find_references
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
from md_mcp.tools.analysis_tools import find_indexed_tool
from md_mcp.tools.resolver_tools import (
    resolve_character_tool,
    resolve_country_tag_tool,
    resolve_scripted_effect_tool,
    resolve_scripted_trigger_tool,
    resolve_trait_tool,
)


def _indexes(root, cache):
    return (
        CountryTagIndex(root, cache, include_vanilla=False),
        CharacterIndex(root, cache, include_vanilla=False),
        TraitIndex(root, cache, include_vanilla=False),
        ScriptedEffectIndex(root, cache, include_vanilla=False),
        ScriptedTriggerIndex(root, cache, include_vanilla=False),
    )


def test_definition_indexes_resolve_fixture_records(fake_mod_root, cache_dir):
    tags, characters, traits, effects, triggers = _indexes(fake_mod_root, cache_dir)

    assert tags.resolve("TST")["country_file"] == "countries/Testland.txt"
    assert characters.resolve("TST_test_character")["kind"] == "character"
    assert traits.resolve("TST_test_trait")["kind"] == "trait"
    assert effects.resolve("TST_test_effect")["kind"] == "scripted_effect"
    assert triggers.resolve("TST_test_trigger")["kind"] == "scripted_trigger"


@pytest.mark.integration
def test_real_mod_definition_indexes(real_mod_root, tmp_path):
    """Smoke-test the indexes against the history-scale sibling checkout."""
    tags, characters, traits, effects, triggers = _indexes(real_mod_root, tmp_path / "cache")
    assert len(tags.list_keys()) > 100
    assert len(characters.list_keys()) > 100
    assert len(traits.list_keys()) > 100
    assert len(effects.list_keys()) > 100
    assert len(triggers.list_keys()) > 50


def test_find_references_supports_new_kinds(fake_mod_root):
    scope = fake_mod_root / "common" / "national_focus" / "TST_new_refs.txt"
    scope.write_text(
        """focus_tree = {
    focus = {
        id = TST_new_refs
        original_tag = TST
        character = TST_test_character
        add_trait = TST_test_trait
        TST_test_effect = {}
        TST_test_trigger = {}
    }
}
""",
        encoding="utf-8",
    )
    cases: tuple[tuple[Kind, str], ...] = (
        ("country_tag", "TST"),
        ("character", "TST_test_character"),
        ("trait", "TST_test_trait"),
        ("scripted_effect", "TST_test_effect"),
        ("scripted_trigger", "TST_test_trigger"),
    )
    for kind, target in cases:
        result = find_references(fake_mod_root, kind, target)
        assert result["ok"]
        assert result["total"] >= 1


def test_check_refs_resolves_new_definition_kinds(fake_mod_root, cache_dir):
    scope = fake_mod_root / "common" / "national_focus" / "TST_new_refs.txt"
    scope.write_text(
        """focus_tree = {
    focus = {
        id = TST_new_refs
        original_tag = TST
        character = TST_test_character
        add_trait = TST_test_trait
        TST_test_effect = {}
        TST_test_trigger = {}
    }
}
""",
        encoding="utf-8",
    )
    tags, characters, traits, effects, triggers = _indexes(fake_mod_root, cache_dir)
    result = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_new_refs.txt"],
        kinds=["country_tag", "character", "trait", "scripted_effect", "scripted_trigger"],
        country_tag_index=tags,
        character_index=characters,
        trait_index=traits,
        scripted_effect_index=effects,
        scripted_trigger_index=triggers,
        focus_index=FocusIndex(fake_mod_root, cache_dir, include_vanilla=False),
        event_index=EventIndex(fake_mod_root, cache_dir, include_vanilla=False),
        idea_index=IdeaIndex(fake_mod_root, cache_dir, include_vanilla=False),
        gfx_index=GfxIndex(fake_mod_root, cache_dir, include_vanilla=False),
        loc_index=LocalisationIndex(fake_mod_root, cache_dir, include_vanilla=False),
        decision_index=DecisionIndex(fake_mod_root, cache_dir, include_vanilla=False),
    )
    assert result["total_unresolved"] == 0


def test_manifest_includes_new_definition_categories(fake_mod_root, cache_dir):
    tags, characters, traits, effects, triggers = _indexes(fake_mod_root, cache_dir)
    result = list_country_content(
        "TST",
        fake_mod_root,
        country_tag_index=tags,
        character_index=characters,
        trait_index=traits,
        scripted_effect_index=effects,
        scripted_trigger_index=triggers,
        include=["country_tags", "characters", "character_files", "traits"],
    )
    assert result["country_tags"] == ["TST"]
    assert result["characters"] == ["TST_test_character"]
    assert result["character_files"] == ["common/characters/TST.txt"]
    assert result["traits"] == ["TST_test_trait"]


def test_definition_resolvers_and_finder_are_budgeted(fake_mod_root, cache_dir):
    tags, characters, traits, effects, triggers = _indexes(fake_mod_root, cache_dir)

    assert resolve_country_tag_tool("TST", tags)["ok"]
    assert resolve_character_tool("TST_test_character", characters)["ok"]
    assert resolve_trait_tool("TST_test_trait", traits)["ok"]
    assert resolve_scripted_effect_tool("TST_test_effect", effects)["ok"]
    assert resolve_scripted_trigger_tool("TST_test_trigger", triggers)["ok"]

    tag_matches = find_indexed_tool(tags, query="TST")["matches"]
    assert tag_matches[0]["country_file"] == "countries/Testland.txt"
    result = find_indexed_tool(effects, query="test", limit=1)
    assert result["total"] == 1
    assert result["returned"] == 1
    assert result["truncated"] is False
