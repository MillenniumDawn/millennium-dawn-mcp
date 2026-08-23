"""Resource handler tests — anchoring decision/idea extraction to the index (issue #31)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from md_mcp.config import Settings
from md_mcp.indexes import DecisionIndex, EventIndex, GfxIndex, IdeaIndex
from md_mcp.paradox.nodes import Node, SymbolNode
from md_mcp.resources import (
    _extract_focus_block,
    decision_resource,
    event_resource,
    idea_resource,
    sprite_resource,
)


def _settings(mod_root: Path, cache_dir: Path) -> Settings:
    return Settings(mod_root=mod_root, vanilla_path=None, cache_dir=cache_dir)


def _write_decisions(tmp_path: Path, text: str) -> Path:
    mod_root = tmp_path / "Mod"
    decisions_dir = mod_root / "common" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "test.txt").write_text(text, encoding="utf-8")
    return mod_root


def _write_ideas(tmp_path: Path, text: str) -> Path:
    mod_root = tmp_path / "Mod"
    ideas_dir = mod_root / "common" / "ideas"
    ideas_dir.mkdir(parents=True)
    (ideas_dir / "test.txt").write_text(text, encoding="utf-8")
    return mod_root


def _write_events(tmp_path: Path, text: str) -> Path:
    mod_root = tmp_path / "Mod"
    events_dir = mod_root / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "test.txt").write_text(text, encoding="utf-8")
    return mod_root


def _write_sprites(tmp_path: Path, text: str) -> Path:
    mod_root = tmp_path / "Mod"
    interface_dir = mod_root / "interface"
    interface_dir.mkdir(parents=True)
    (interface_dir / "test.gfx").write_text(text, encoding="utf-8")
    return mod_root


class _FakeIndex:
    """Duck-typed stand-in for DecisionIndex/IdeaIndex that resolves to a fixed record."""

    def __init__(self, rec: dict):
        self._rec = rec

    def resolve(self, key: str) -> dict:
        return self._rec


DECISIONS_WITH_IMPOSTOR = """TST_category = {
\ticon = generic_decision_category

\tTST_impostor_home = {
\t\tallowed = { tag = TST }
\t\tcomplete_effect = {
\t\t\tTST_real = {
\t\t\t\tsome_effect = yes
\t\t\t}
\t\t\tadd_political_power = 50
\t\t}
\t}

\tTST_real = {
\t\tallowed = { tag = TST }
\t\tcost = 25
\t}
}
"""

IDEAS_WITH_IMPOSTOR = """ideas = {
\tcountry = {
\t\tTST_impostor_home = {
\t\t\tmodifier = {
\t\t\t\tTST_real = {
\t\t\t\t\tstability_factor = 0.05
\t\t\t\t}
\t\t\t}
\t\t\tallowed = { original_tag = TST }
\t\t}

\t\tTST_real = {
\t\t\tpicture = generic_idea
\t\t\tmodifier = {
\t\t\t\twar_support_factor = 0.05
\t\t\t}
\t\t}
\t}
}
"""

DECISIONS_WITH_DUPES = """TST_category = {
\tTST_dup = {
\t\tcost = 10
\t}

\tTST_dup = {
\t\tcost = 20
\t}
}
"""

DECISIONS_WITH_COMMENT = (
    "TST_category = {\n" "\tTST_commented = {\n" "\t\tcost = 30 # important note\n" "\t}\n" "}\n"
)

EVENTS_WITH_IMPOSTOR = """add_namespace = TST

country_event = {
\tid = TST.1
\ttitle = TST.1.t
\timmediate = {
\t\tcountry_event = {
\t\t\tid = TST.2
\t\t\ttitle = impostor.t
\t\t}
\t}
}

country_event = {
\tid = TST.2
\ttitle = TST.2.t
\tdesc = TST.2.d
}
"""

EVENTS_WITH_DUPES = """add_namespace = TST

country_event = {
\tid = TST.1
\ttitle = TST.1.t_first
}

country_event = {
\tid = TST.1
\ttitle = TST.1.t_second
}
"""

EVENTS_WITH_COMMENT = (
    "add_namespace = TST\n"
    "\n"
    "country_event = {\n"
    "\tid = TST.1\n"
    "\ttitle = TST.1.t # important note\n"
    "}\n"
)

SPRITES_WITH_IMPOSTOR = """spriteTypes = {
\tspriteType = {
\t\tname = "GFX_impostor_home"
\t\ttexturefile = "gfx/interface/impostor.dds"
\t\tsomeBlock = {
\t\t\tspriteType = {
\t\t\t\tname = "GFX_real"
\t\t\t\ttexturefile = "gfx/interface/decoy.dds"
\t\t\t}
\t\t}
\t}

\tspriteType = {
\t\tname = "GFX_real"
\t\ttexturefile = "gfx/interface/real.dds"
\t}
}
"""

SPRITES_WITH_DUPES = """spriteTypes = {
\tspriteType = {
\t\tname = "GFX_dup"
\t\ttexturefile = "gfx/interface/first.dds"
\t}

\tspriteType = {
\t\tname = "GFX_dup"
\t\ttexturefile = "gfx/interface/second.dds"
\t}
}
"""

SPRITES_WITH_COMMENT = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_commented"\n'
    '\t\ttexturefile = "gfx/interface/real.dds" # important note\n'
    "\t}\n"
    "}\n"
)

EVENTS_WITH_IMPOSTOR_SANDWICH = """add_namespace = TST

country_event = {
\tid = TST.9
\ttitle = TST.9.t_before
\timmediate = {
\t\tcountry_event = {
\t\t\tid = TST.5
\t\t\ttitle = impostor_before.t
\t\t}
\t}
}

country_event = {
\tid = TST.5
\ttitle = TST.5.t_real
}

country_event = {
\tid = TST.9
\ttitle = TST.9.t_after
\timmediate = {
\t\tcountry_event = {
\t\t\tid = TST.5
\t\t\ttitle = impostor_after.t
\t\t}
\t}
}
"""

SPRITES_WITH_IMPOSTOR_SANDWICH = """spriteTypes = {
\tspriteType = {
\t\tname = "GFX_before"
\t\ttexturefile = "gfx/interface/before.dds"
\t\tsomeBlock = {
\t\t\tspriteType = {
\t\t\t\tname = "GFX_mid"
\t\t\t\ttexturefile = "gfx/interface/impostor_before.dds"
\t\t\t}
\t\t}
\t}

\tspriteType = {
\t\tname = "GFX_mid"
\t\ttexturefile = "gfx/interface/real.dds"
\t}

\tspriteType = {
\t\tname = "GFX_after"
\t\ttexturefile = "gfx/interface/after.dds"
\t\tsomeBlock = {
\t\t\tspriteType = {
\t\t\t\tname = "GFX_mid"
\t\t\t\ttexturefile = "gfx/interface/impostor_after.dds"
\t\t\t}
\t\t}
\t}
}
"""

SPRITES_WITH_BARE_AND_QUOTED_NAMES = """spriteTypes = {
\tspriteType = {
\t\tname = "GFX_quoted"
\t\ttexturefile = "gfx/interface/quoted.dds"
\t}
\tspriteType = {
\t\tname = GFX_bare
\t\ttexturefile = "gfx/interface/bare.dds"
\t}
}
"""


def test_decision_resource_returns_real_definition_not_nested_impostor(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_IMPOSTOR)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = DecisionIndex(mod_root, settings.cache_dir)

    result = decision_resource("TST_real", settings, index)

    assert "cost = 25" in result
    assert "some_effect" not in result


def test_idea_resource_returns_real_definition_not_nested_impostor(tmp_path):
    mod_root = _write_ideas(tmp_path, IDEAS_WITH_IMPOSTOR)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = IdeaIndex(mod_root, settings.cache_dir)

    result = idea_resource("TST_real", settings, index)

    assert "war_support_factor = 0.05" in result
    assert "stability_factor" not in result


def test_decision_resource_duplicate_ids_anchored_by_indexed_line(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_DUPES)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = DecisionIndex(mod_root, settings.cache_dir)

    result = decision_resource("TST_dup", settings, index)

    # The index keys the last occurrence in file order; the resource must match it.
    assert "cost = 20" in result
    assert "cost = 10" not in result


def test_decision_resource_duplicate_ids_without_line_raises_ambiguous(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_DUPES)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = DecisionIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("TST_dup")
    assert rec is not None
    fake_index = _FakeIndex({**rec, "line": None})

    with pytest.raises(KeyError, match="ambiguous"):
        decision_resource("TST_dup", settings, cast(DecisionIndex, fake_index))


def test_decision_resource_single_match_without_line_still_resolves(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = DecisionIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("TST_commented")
    assert rec is not None
    fake_index = _FakeIndex({**rec, "line": None})

    result = decision_resource("TST_commented", settings, cast(DecisionIndex, fake_index))

    assert "cost = 30" in result


def test_decision_resource_stale_index_line_raises_with_rebuild_hint(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = DecisionIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("TST_commented")
    assert rec is not None
    # Point the record at the category header line, which is not a decision definition.
    fake_index = _FakeIndex({**rec, "line": 1})

    with pytest.raises(KeyError, match=r"stale.*build-index"):
        decision_resource("TST_commented", settings, cast(DecisionIndex, fake_index))


def test_decision_resource_exact_source_preserved_with_comment(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = DecisionIndex(mod_root, settings.cache_dir)

    result = decision_resource("TST_commented", settings, index)

    expected = "\tTST_commented = {\n\t\tcost = 30 # important note\n\t}"
    assert result == expected


def test_event_resource_returns_real_definition_not_nested_impostor(tmp_path):
    mod_root = _write_events(tmp_path, EVENTS_WITH_IMPOSTOR)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = EventIndex(mod_root, settings.cache_dir)

    result = event_resource("TST.2", settings, index)

    assert "title = TST.2.t" in result
    assert "impostor.t" not in result


def test_event_resource_duplicate_ids_anchored_by_indexed_line(tmp_path):
    mod_root = _write_events(tmp_path, EVENTS_WITH_DUPES)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = EventIndex(mod_root, settings.cache_dir)

    result = event_resource("TST.1", settings, index)

    # The index keys the last occurrence in file order; the resource must match it.
    assert "title = TST.1.t_second" in result
    assert "t_first" not in result


def test_event_resource_duplicate_ids_without_line_raises_ambiguous(tmp_path):
    mod_root = _write_events(tmp_path, EVENTS_WITH_DUPES)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = EventIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("TST.1")
    assert rec is not None
    fake_index = _FakeIndex({**rec, "line": None})

    with pytest.raises(KeyError, match="ambiguous"):
        event_resource("TST.1", settings, cast(EventIndex, fake_index))


def test_event_resource_stale_index_line_raises_with_rebuild_hint(tmp_path):
    mod_root = _write_events(tmp_path, EVENTS_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = EventIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("TST.1")
    assert rec is not None
    # Point the record at the add_namespace line, which is not an event definition.
    fake_index = _FakeIndex({**rec, "line": 1})

    with pytest.raises(KeyError, match=r"stale.*build-index"):
        event_resource("TST.1", settings, cast(EventIndex, fake_index))


def test_event_resource_exact_source_preserved_with_comment(tmp_path):
    mod_root = _write_events(tmp_path, EVENTS_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = EventIndex(mod_root, settings.cache_dir)

    result = event_resource("TST.1", settings, index)

    expected = "country_event = {\n\tid = TST.1\n\ttitle = TST.1.t # important note\n}"
    assert result == expected


def test_event_resource_impostor_excluded_from_candidates_without_line(tmp_path):
    # Even when the index has no line to disambiguate with, the nested impostor must
    # never be a candidate at all -- only the real top-level event is in scope.
    mod_root = _write_events(tmp_path, EVENTS_WITH_IMPOSTOR)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = EventIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("TST.2")
    assert rec is not None
    fake_index = _FakeIndex({**rec, "line": None})

    result = event_resource("TST.2", settings, cast(EventIndex, fake_index))

    assert "title = TST.2.t" in result
    assert "impostor.t" not in result


def test_event_resource_anchors_middle_definition_sandwiched_by_impostors(tmp_path):
    mod_root = _write_events(tmp_path, EVENTS_WITH_IMPOSTOR_SANDWICH)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = EventIndex(mod_root, settings.cache_dir)

    result = event_resource("TST.5", settings, index)

    assert "title = TST.5.t_real" in result
    assert "impostor_before" not in result
    assert "impostor_after" not in result


def test_sprite_resource_returns_real_definition_not_nested_impostor(tmp_path):
    mod_root = _write_sprites(tmp_path, SPRITES_WITH_IMPOSTOR)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = GfxIndex(mod_root, settings.cache_dir)

    result = sprite_resource("GFX_real", settings, index)

    assert "gfx/interface/real.dds" in result
    assert "decoy.dds" not in result


def test_sprite_resource_duplicate_names_anchored_by_indexed_line(tmp_path):
    mod_root = _write_sprites(tmp_path, SPRITES_WITH_DUPES)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = GfxIndex(mod_root, settings.cache_dir)

    result = sprite_resource("GFX_dup", settings, index)

    # The index keys the last occurrence in file order; the resource must match it.
    assert "gfx/interface/second.dds" in result
    assert "first.dds" not in result


def test_sprite_resource_duplicate_names_without_line_raises_ambiguous(tmp_path):
    mod_root = _write_sprites(tmp_path, SPRITES_WITH_DUPES)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = GfxIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("GFX_dup")
    assert rec is not None
    fake_index = _FakeIndex({**rec, "line": None})

    with pytest.raises(KeyError, match="ambiguous"):
        sprite_resource("GFX_dup", settings, cast(GfxIndex, fake_index))


def test_sprite_resource_stale_index_line_raises_with_rebuild_hint(tmp_path):
    mod_root = _write_sprites(tmp_path, SPRITES_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = GfxIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("GFX_commented")
    assert rec is not None
    # Point the record at the spriteTypes header line, which is not a sprite definition.
    fake_index = _FakeIndex({**rec, "line": 1})

    with pytest.raises(KeyError, match=r"stale.*build-index"):
        sprite_resource("GFX_commented", settings, cast(GfxIndex, fake_index))


def test_sprite_resource_exact_source_preserved_with_comment(tmp_path):
    mod_root = _write_sprites(tmp_path, SPRITES_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = GfxIndex(mod_root, settings.cache_dir)

    result = sprite_resource("GFX_commented", settings, index)

    expected = (
        '\tspriteType = {\n\t\tname = "GFX_commented"\n'
        '\t\ttexturefile = "gfx/interface/real.dds" # important note\n\t}'
    )
    assert result == expected


def test_sprite_resource_impostor_excluded_from_candidates_without_line(tmp_path):
    # Same guard as events: with no line to disambiguate, the deeply nested impostor
    # must never even be a candidate.
    mod_root = _write_sprites(tmp_path, SPRITES_WITH_IMPOSTOR)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = GfxIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("GFX_real")
    assert rec is not None
    fake_index = _FakeIndex({**rec, "line": None})

    result = sprite_resource("GFX_real", settings, cast(GfxIndex, fake_index))

    assert "gfx/interface/real.dds" in result
    assert "decoy.dds" not in result


def test_sprite_resource_anchors_middle_definition_sandwiched_by_impostors(tmp_path):
    # Force line=None so this exercises find_sprite_nodes' hierarchy restriction
    # directly: the two nested impostors (before and after) must never be candidates.
    mod_root = _write_sprites(tmp_path, SPRITES_WITH_IMPOSTOR_SANDWICH)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = GfxIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("GFX_mid")
    assert rec is not None
    fake_index = _FakeIndex({**rec, "line": None})

    result = sprite_resource("GFX_mid", settings, cast(GfxIndex, fake_index))

    assert "gfx/interface/real.dds" in result
    assert "impostor_before.dds" not in result
    assert "impostor_after.dds" not in result


def test_sprite_resource_nested_impostor_after_real_resolved_by_index_line(tmp_path):
    mod_root = _write_sprites(tmp_path, SPRITES_WITH_IMPOSTOR_SANDWICH)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = GfxIndex(mod_root, settings.cache_dir)

    result = sprite_resource("GFX_mid", settings, index)

    assert "gfx/interface/real.dds" in result
    assert "impostor_before.dds" not in result
    assert "impostor_after.dds" not in result


def test_sprite_resource_anchors_bare_and_quoted_names(tmp_path):
    mod_root = _write_sprites(tmp_path, SPRITES_WITH_BARE_AND_QUOTED_NAMES)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = GfxIndex(mod_root, settings.cache_dir)

    quoted = sprite_resource("GFX_quoted", settings, index)
    bare = sprite_resource("GFX_bare", settings, index)

    assert "gfx/interface/quoted.dds" in quoted
    assert "gfx/interface/bare.dds" in bare


def test_extract_focus_block_malformed_node_raises_with_focus_id(monkeypatch):
    focus_id = "TST_malformed"
    malformed_focus = Node(
        name="focus",
        value=[Node(name="id", value=SymbolNode(focus_id))],
    )
    root = Node(value=[Node(name="focus_tree", value=[malformed_focus])])
    monkeypatch.setattr("md_mcp.resources.parse_string", lambda _text: root)

    with pytest.raises(KeyError, match=rf"Focus '{focus_id}'.*malformed parse"):
        _extract_focus_block("", focus_id)
