"""Tests for the M2 indexes: GFX, event, decision, idea."""

from __future__ import annotations

from md_mcp.indexes import DecisionIndex, EventIndex, GfxIndex, IdeaIndex


def test_gfx_index_builds(fake_mod_root, cache_dir):
    g = GfxIndex(fake_mod_root, cache_dir, include_vanilla=False)
    g.ensure_fresh()
    names = g.list_keys()
    assert "GFX_test_sprite_one" in names
    assert "GFX_test_sprite_two" in names
    assert "GFX_test_tile" in names

    rec = g.resolve("GFX_test_sprite_one")
    assert rec is not None
    assert rec["texturefile"] == "gfx/test/one.dds"
    assert rec["kind"] == "spriteType"
    assert rec["file"].endswith("test_sprites.gfx")


def test_event_index_builds(fake_mod_root, cache_dir):
    ev = EventIndex(fake_mod_root, cache_dir, include_vanilla=False)
    ev.ensure_fresh()
    assert "Testns.1" in ev.list_keys()
    assert "TestnsNews.42" in ev.list_keys()

    rec = ev.resolve("Testns.1")
    assert rec is not None
    assert rec["kind"] == "country_event"
    assert rec["namespace"] == "Testns"
    assert "Testns" in rec["file_namespaces"]
    assert "TestnsNews" in rec["file_namespaces"]


def test_decision_index_builds(fake_mod_root, cache_dir):
    d = DecisionIndex(fake_mod_root, cache_dir, include_vanilla=False)
    d.ensure_fresh()
    assert "TST_simple_decision" in d.list_keys()
    assert "TST_targeted_decision" in d.list_keys()

    rec = d.resolve("TST_simple_decision")
    assert rec is not None
    assert rec["category"] == "TST_category"
    assert rec["file"].endswith("test_decisions.txt")


def test_decision_index_skips_category_keywords(fake_mod_root, cache_dir):
    """`icon`, `priority` etc. inside a decision category must NOT register as decisions."""
    d = DecisionIndex(fake_mod_root, cache_dir, include_vanilla=False)
    keys = set(d.list_keys())
    assert "icon" not in keys
    assert "priority" not in keys


def test_idea_index_builds(fake_mod_root, cache_dir):
    i = IdeaIndex(fake_mod_root, cache_dir, include_vanilla=False)
    i.ensure_fresh()
    assert "TST_simple_idea" in i.list_keys()
    assert "TST_another_idea" in i.list_keys()
    # Companies sit one level deeper (inside a slot wrapper).
    assert "TST_acme_tanks" in i.list_keys()


def test_idea_index_categories(fake_mod_root, cache_dir):
    i = IdeaIndex(fake_mod_root, cache_dir, include_vanilla=False)
    simple = i.resolve("TST_simple_idea")
    acme = i.resolve("TST_acme_tanks")
    assert simple is not None and simple["category"] == "country"
    assert acme is not None and acme["category"] == "tank_manufacturer"
    # The slot wrapper itself (designer / law) must NOT be indexed as an idea.
    assert "designer" not in i.list_keys()
