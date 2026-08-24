"""Tests for check_refs — scoped cross-reference audit."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from md_mcp.analysis.ref_audit import check_refs
from md_mcp.indexes import (
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
)

# References real fixture content from conftest's fake_mod_root:
#   events_minimal.txt defines Testns.1 / TestnsNews.42
#   ideas_minimal.txt defines TST_simple_idea
#   sprites_minimal.gfx defines GFX_test_sprite_one
#   test_l_english.yml has some keys (not the focus ids below)
_FOCUS_FILE = """focus_tree = {
    id = TST_audit_tree
    focus = {
        id = TST_audit_root
        x = 1
        y = 0
        icon = GFX_test_sprite_one
        completion_reward = {
            country_event = Testns.1
            country_event = { id = Missing.99 days = 3 }
            add_ideas = TST_simple_idea
            add_ideas = { TST_missing_idea }
        }
    }
    focus = {
        id = TST_audit_child
        x = 1
        y = 1
        icon = GFX_missing_sprite
        prerequisite = { focus = TST_audit_root focus = TST_missing_focus }
        relative_position_id = TST_audit_root
        available = { has_completed_focus = TST_other_missing }
    }
}
"""


def _indexes(root: Path, cache: Path) -> dict:
    return {
        "focus_index": FocusIndex(root, cache, None),
        "event_index": EventIndex(root, cache, None),
        "idea_index": IdeaIndex(root, cache, None),
        "gfx_index": GfxIndex(root, cache, None),
        "loc_index": LocalisationIndex(root, cache, None),
        "decision_index": DecisionIndex(root, cache, None),
    }


@pytest.fixture
def audit_mod(fake_mod_root, cache_dir):
    f = fake_mod_root / "common" / "national_focus" / "TST_audit.txt"
    f.write_text(_FOCUS_FILE, encoding="utf-8")
    return fake_mod_root, cache_dir


def test_signature():
    params = inspect.signature(check_refs).parameters
    for p in ("tag", "files", "kinds", "limit", "offset", "counts_only", "lang"):
        assert p in params


def test_requires_scope(fake_mod_root, cache_dir):
    out = check_refs(fake_mod_root, **_indexes(fake_mod_root, cache_dir))
    assert out["ok"] is False


def test_unknown_kind_rejected(fake_mod_root, cache_dir):
    out = check_refs(
        fake_mod_root, files=["x.txt"], kinds=["bogus"], **_indexes(fake_mod_root, cache_dir)
    )
    assert out["ok"] is False
    assert "bogus" in out["error"]


def test_audit_finds_dangling_refs(audit_mod):
    root, cache = audit_mod
    out = check_refs(
        root,
        files=["common/national_focus/TST_audit.txt"],
        **_indexes(root, cache),
    )
    assert out["ok"] is True
    unresolved = {(e["kind"], e["ref"]) for e in out["unresolved"]}

    assert ("event", "Missing.99") in unresolved
    assert ("event", "Testns.1") not in unresolved
    assert ("idea", "TST_missing_idea") in unresolved
    assert ("idea", "TST_simple_idea") not in unresolved
    assert ("sprite", "GFX_missing_sprite") in unresolved
    assert ("sprite", "GFX_test_sprite_one") not in unresolved
    assert ("focus", "TST_missing_focus") in unresolved
    assert ("focus", "TST_other_missing") in unresolved
    assert ("focus", "TST_audit_root") not in unresolved
    # Focus ids defined in scope but with no loc entries.
    assert ("loc", "TST_audit_root") in unresolved
    assert ("loc", "TST_audit_root_desc") in unresolved


def test_sites_carry_file_line_and_referrer(audit_mod):
    root, cache = audit_mod
    out = check_refs(root, files=["common/national_focus/TST_audit.txt"], **_indexes(root, cache))
    entry = next(e for e in out["unresolved"] if e["ref"] == "TST_missing_focus")
    site = entry["sites"][0]
    assert site["file"] == "common/national_focus/TST_audit.txt"
    assert isinstance(site["line"], int) and site["line"] > 1
    assert site["via"] == "prerequisite"
    assert site["referrer"] == "TST_audit_child"


def test_kinds_subset(audit_mod):
    root, cache = audit_mod
    out = check_refs(
        root,
        files=["common/national_focus/TST_audit.txt"],
        kinds=["event"],
        **_indexes(root, cache),
    )
    assert out["kinds_checked"] == ["event"]
    assert all(e["kind"] == "event" for e in out["unresolved"])
    assert set(out["counts"]) == {"event"}


def test_counts_only_and_pagination(audit_mod):
    root, cache = audit_mod
    idx = _indexes(root, cache)
    out = check_refs(root, files=["common/national_focus/TST_audit.txt"], counts_only=True, **idx)
    assert "unresolved" not in out
    assert out["total_unresolved"] > 0

    page = check_refs(root, files=["common/national_focus/TST_audit.txt"], limit=2, offset=0, **idx)
    assert len(page["unresolved"]) == 2
    assert page["truncated"] is True


def test_tag_scope(audit_mod):
    root, cache = audit_mod
    out = check_refs(root, tag="TST_audit", **_indexes(root, cache))
    # tag prefix TST_AUDIT_ matches both focuses in the file
    assert out["ok"] is True
    assert out["scope"] == {"tag": "TST_AUDIT"}
    assert out["files_scanned"] == 1


def test_dedup_counts_occurrences(fake_mod_root, cache_dir):
    body = """focus_tree = {
    focus = {
        id = TST_dup
        x = 1
        y = 0
        completion_reward = {
            country_event = Gone.1
            country_event = Gone.1
        }
    }
}
"""
    f = fake_mod_root / "common" / "national_focus" / "TST_dup.txt"
    f.write_text(body, encoding="utf-8")
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_dup.txt"],
        kinds=["event"],
        **_indexes(fake_mod_root, cache_dir),
    )
    entry = next(e for e in out["unresolved"] if e["ref"] == "Gone.1")
    assert entry["count"] == 2
    assert len(entry["sites"]) == 2


def test_vanilla_flag_surfaced(audit_mod):
    root, cache = audit_mod
    out = check_refs(
        root,
        files=["common/national_focus/TST_audit.txt"],
        counts_only=True,
        **_indexes(root, cache),
    )
    assert out["vanilla_indexed"] is False
    assert out["vanilla_manifest"] is False
    assert "scripted_effects" in out["not_checked"]


_MANIFEST_FOCUS = """focus_tree = {
    focus = {
        id = TST_manifest_root
        x = 1
        y = 0
        icon = GFX_vanilla_only_sprite
        completion_reward = { country_event = Testns.1 }
    }
}
"""


def _write_manifest_focus(root: Path) -> None:
    (root / "common" / "national_focus" / "TST_manifest_sprite.txt").write_text(
        _MANIFEST_FOCUS, encoding="utf-8"
    )


def test_sprite_ref_resolved_from_manifest(fake_mod_root, cache_dir):
    """A vanilla-only sprite referenced in scope resolves via the manifest, not as unresolved."""
    _write_manifest_focus(fake_mod_root)
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_manifest_sprite.txt"],
        kinds=["sprite"],
        counts_only=True,
        vanilla_sprites=frozenset({"GFX_vanilla_only_sprite"}),
        **_indexes(fake_mod_root, cache_dir),
    )
    assert out["vanilla_manifest"] is True
    assert out["counts"]["sprite"]["unresolved"] == 0


def test_sprite_ref_unresolved_without_manifest(fake_mod_root, cache_dir):
    """The same vanilla-only sprite is flagged unresolved when no manifest is given."""
    _write_manifest_focus(fake_mod_root)
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_manifest_sprite.txt"],
        kinds=["sprite"],
        counts_only=True,
        **_indexes(fake_mod_root, cache_dir),
    )
    assert out["vanilla_manifest"] is False
    assert out["counts"]["sprite"]["unresolved"] == 1


def test_sprite_ref_resolved_via_gfx_prefix_manifest(fake_mod_root, cache_dir):
    """A bare sprite id resolves when the manifest holds the GFX_<id> form (HOI4 prefix rule)."""
    (fake_mod_root / "common" / "national_focus" / "TST_bare_sprite.txt").write_text(
        """focus_tree = {
    focus = {
        id = TST_bare_root
        x = 1
        y = 0
        icon = vanilla_icon_bare
        completion_reward = { country_event = Testns.1 }
    }
}
""",
        encoding="utf-8",
    )
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_bare_sprite.txt"],
        kinds=["sprite"],
        counts_only=True,
        vanilla_sprites=frozenset({"GFX_vanilla_icon_bare"}),
        **_indexes(fake_mod_root, cache_dir),
    )
    assert out["vanilla_manifest"] is True
    assert out["counts"]["sprite"]["unresolved"] == 0


def test_sprite_manifest_flag_absent_without_manifest(audit_mod):
    root, cache = audit_mod
    out = check_refs(
        root,
        files=["common/national_focus/TST_audit.txt"],
        kinds=["sprite"],
        counts_only=True,
        **_indexes(root, cache),
    )
    assert out["vanilla_manifest"] is False


def test_scope_file_resolved_from_vanilla(fake_mod_root, cache_dir, tmp_path):
    """A scope path that lives only in vanilla must still be audited, not skipped as 'not found'."""
    vanilla = tmp_path / "vanilla"
    vf = vanilla / "common" / "national_focus" / "vanilla_only.txt"
    vf.parent.mkdir(parents=True)
    vf.write_text(
        """focus_tree = {
    focus = {
        id = TST_vanilla_focus
        x = 0
        y = 0
        completion_reward = { country_event = VanillaGone.1 }
    }
}
""",
        encoding="utf-8",
    )
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/vanilla_only.txt"],
        kinds=["event"],
        vanilla_path=vanilla,
        **_indexes(fake_mod_root, cache_dir),
    )
    assert out["ok"] is True
    assert "parse_errors" not in out  # found in vanilla, so parsed rather than skipped
    assert ("event", "VanillaGone.1") in {(e["kind"], e["ref"]) for e in out["unresolved"]}


def test_scope_file_not_found_anywhere(fake_mod_root, cache_dir):
    """A path in neither mod nor vanilla is still reported as not found."""
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/does_not_exist.txt"],
        **_indexes(fake_mod_root, cache_dir),
    )
    assert out["parse_errors"] == [
        {"file": "common/national_focus/does_not_exist.txt", "error": "not found"}
    ]


def test_idea_picture_resolves_as_gfx_idea(fake_mod_root, cache_dir):
    """HOI4 resolves idea `picture` fields as GFX_idea_<picture>, not GFX_<picture>."""
    (fake_mod_root / "interface").mkdir(exist_ok=True)
    (fake_mod_root / "interface" / "idea_sprites.gfx").write_text(
        (
            "spriteTypes = {\n"
            "\tspriteType = {\n"
            '\t\tname = "GFX_idea_generic_foo"\n'
            '\t\ttexturefile = "gfx/foo.dds"\n'
            "\t}\n}\n"
        ),
        encoding="utf-8",
    )
    ideas_file = fake_mod_root / "common" / "ideas" / "TST_ideas.txt"
    ideas_file.parent.mkdir(parents=True, exist_ok=True)
    ideas_file.write_text(
        (
            "ideas = {\n"
            "\tcountries = {\n"
            "\t\tTST_IDEA = {\n"
            "\t\t\tpicture = generic_foo\n"
            "\t\t\tpicture = generic_missing\n"
            "\t\t}\n\t}\n}\n"
        ),
        encoding="utf-8",
    )
    out = check_refs(
        fake_mod_root,
        files=["common/ideas/TST_ideas.txt"],
        kinds=["sprite"],
        **_indexes(fake_mod_root, cache_dir),
    )
    unresolved = {(e["kind"], e["ref"]) for e in out["unresolved"]}
    assert ("sprite", "generic_foo") not in unresolved  # resolved as GFX_idea_generic_foo
    assert ("sprite", "generic_missing") in unresolved


def test_texture_paths_not_sprite_refs(fake_mod_root, cache_dir):
    """picture = foo.dds inside leader-creation effects is a file path, not a sprite id."""
    body = """focus_tree = {
    focus = {
        id = TST_leaderpic
        x = 1
        y = 0
        completion_reward = {
            create_country_leader = {
                name = "Someone"
                picture = some_portrait.dds
            }
        }
    }
}
"""
    f = fake_mod_root / "common" / "national_focus" / "TST_leaderpic.txt"
    f.write_text(body, encoding="utf-8")
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_leaderpic.txt"],
        kinds=["sprite"],
        **_indexes(fake_mod_root, cache_dir),
    )
    assert out["counts"]["sprite"]["checked"] == 0
    assert out["total_unresolved"] == 0
