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


def _indexes(root, cache):
    return (
        CountryTagIndex(root, cache, include_vanilla=False),
        CharacterIndex(root, cache, include_vanilla=False),
        TraitIndex(root, cache, include_vanilla=False),
        ScriptedEffectIndex(root, cache, include_vanilla=False),
        ScriptedTriggerIndex(root, cache, include_vanilla=False),
    )


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


def _audit(root, cache, text, kinds):
    (root / "common" / "national_focus" / "TST_new_refs.txt").write_text(text, encoding="utf-8")
    tags, characters, traits, effects, triggers = _indexes(root, cache)
    return check_refs(
        root,
        files=["common/national_focus/TST_new_refs.txt"],
        kinds=kinds,
        country_tag_index=tags,
        character_index=characters,
        trait_index=traits,
        scripted_effect_index=effects,
        scripted_trigger_index=triggers,
        focus_index=FocusIndex(root, cache, include_vanilla=False),
        event_index=EventIndex(root, cache, include_vanilla=False),
        idea_index=IdeaIndex(root, cache, include_vanilla=False),
        gfx_index=GfxIndex(root, cache, include_vanilla=False),
        loc_index=LocalisationIndex(root, cache, include_vanilla=False),
        decision_index=DecisionIndex(root, cache, include_vanilla=False),
    )


_SCRIPTED_KINDS = ["scripted_effect", "scripted_trigger"]


def test_check_refs_resolves_new_definition_kinds(fake_mod_root, cache_dir):
    result = _audit(
        fake_mod_root,
        cache_dir,
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
        ["country_tag", "character", "trait", *_SCRIPTED_KINDS],
    )
    assert result["total_unresolved"] == 0


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("TST_test_effect = yes", "scripted_effect"),
        ("hidden_effect = { TST_test_effect = yes }", "scripted_effect"),
        ("TST_test_trigger = yes", "scripted_trigger"),
        ("hidden_trigger = { TST_test_trigger = yes }", "scripted_trigger"),
    ],
)
def test_check_refs_resolves_scripted_calls_as_one_kind(fake_mod_root, cache_dir, text, kind):
    """A scripted call resolves when the call key matches the index — we audit
    the *key*, not the scalar value, so `TST_test_effect = yes` doesn't read
    as a dangling reference to `yes`. `hidden_effect` is a built-in HOI4
    effect wrapper; only the inner scripted call is audited.
    """
    result = _audit(fake_mod_root, cache_dir, text, _SCRIPTED_KINDS)

    assert result["total_unresolved"] == 0
    assert {k: c["checked"] for k, c in result["counts"].items()} == {
        "scripted_effect": int(kind == "scripted_effect"),
        "scripted_trigger": int(kind == "scripted_trigger"),
    }


def test_check_refs_does_not_audit_fake_wrapper_calls(fake_mod_root, cache_dir):
    """`call_scripted_effect` / `evaluate_scripted_trigger` aren't HOI4 effect
    or trigger names — they're not audited at all, so neither a defined nor a
    missing scripted effect shows up under them. The dangling test_effect
    is reported only if it appears as a direct call by key.
    """
    body = (
        "call_scripted_effect = TST_missing_effect\n"
        "evaluate_scripted_trigger = TST_missing_trigger"
    )
    result = _audit(
        fake_mod_root,
        cache_dir,
        body,
        _SCRIPTED_KINDS,
    )

    assert result["total_unresolved"] == 0
    # Neither fake wrapper contributes a checked reference.
    assert result["counts"]["scripted_effect"]["checked"] == 0
    assert result["counts"]["scripted_trigger"]["checked"] == 0


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
