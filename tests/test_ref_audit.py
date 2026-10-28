"""Tests for check_refs — scoped cross-reference audit."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from md_mcp.analysis.ref_audit import _is_scope_reference, check_refs
from md_mcp.indexes import (
    CharacterIndex,
    CountryTagIndex,
    DecisionIndex,
    DivisionTemplateIndex,
    DynamicModifierIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
    ScriptedEffectIndex,
    ScriptedTriggerIndex,
    TechnologyIndex,
    TraitIndex,
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


def _indexes(root: Path, cache: Path, submod_root: Path | None = None) -> dict:
    return {
        "focus_index": FocusIndex(root, cache, None, submod_root=submod_root),
        "event_index": EventIndex(root, cache, None, submod_root=submod_root),
        "idea_index": IdeaIndex(root, cache, None, submod_root=submod_root),
        "gfx_index": GfxIndex(root, cache, None, submod_root=submod_root),
        "loc_index": LocalisationIndex(root, cache, None, submod_root=submod_root),
        "decision_index": DecisionIndex(root, cache, None, submod_root=submod_root),
    }


@pytest.fixture
def audit_mod(fake_mod_root, cache_dir):
    f = fake_mod_root / "common" / "national_focus" / "TST_audit.txt"
    f.write_text(_FOCUS_FILE, encoding="utf-8")
    return fake_mod_root, cache_dir


def test_requires_scope(fake_mod_root, cache_dir):
    out = check_refs(fake_mod_root, **_indexes(fake_mod_root, cache_dir))
    assert out["ok"] is False


def test_content_definition_refs_resolve_with_explicit_kinds(fake_mod_root, cache_dir):
    root = fake_mod_root
    (root / "common/technologies").mkdir(parents=True, exist_ok=True)
    (root / "common/technologies/test.txt").write_text(
        "technologies = { known_tech = { cost = 1 } }", encoding="utf-8"
    )
    (root / "common/dynamic_modifiers").mkdir(parents=True, exist_ok=True)
    (root / "common/dynamic_modifiers/test.txt").write_text(
        "known_dynamic = { icon = 1 }", encoding="utf-8"
    )
    (root / "history/units").mkdir(parents=True, exist_ok=True)
    (root / "history/units/TST_2000.txt").write_text(
        'division_template = { name = "Infantry" }\n'
        'units = { division = { name = Army division_template = "Infantry" } }',
        encoding="utf-8",
    )
    (root / "history/countries").mkdir(parents=True, exist_ok=True)
    (root / "history/countries/TST.txt").write_text(
        'set_technology = { known_tech = 1 }\noob = "TST_2000"', encoding="utf-8"
    )
    scope = root / "common/national_focus/refs.txt"
    scope.write_text(
        "focus = {\n" " add_dynamic_modifier = { modifier = known_dynamic days = 30 }\n" "}\n",
        encoding="utf-8",
    )
    indexes = _indexes(root, cache_dir)
    registry_indexes = {
        "technology_index": TechnologyIndex(root, cache_dir, None),
        "dynamic_modifier_index": DynamicModifierIndex(root, cache_dir, None),
        "unit_history_index": DivisionTemplateIndex(root, cache_dir, None),
    }
    result = check_refs(
        root,
        files=[
            "common/national_focus/refs.txt",
            "history/countries/TST.txt",
            "history/units/TST_2000.txt",
        ],
        kinds=[
            "technology",
            "dynamic_modifier",
            "oob",
            "division_template",
        ],
        **indexes,
        registry_indexes=registry_indexes,
    )
    assert result["ok"] is True
    assert result["total_unresolved"] == 0


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
    # Hand-counted from _FOCUS_FILE: the `prerequisite = { ... }` line.
    assert site["line"] == 20
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


def test_duplicate_icons_groups_focus_sites_and_paginates(audit_mod):
    root, cache = audit_mod
    path = root / "common" / "national_focus" / "TST_audit.txt"
    path.write_text(
        """focus_tree = {
    focus = {
        id = TST_icon_one
        icon = GFX_shared
    }
    focus = {
        id = TST_icon_two
        icon = gfx_shared
    }
    focus = {
        id = TST_icon_three
        icon = GFX_unique
    }
    focus = {
        id = TST_icon_four
        icon = GFX_zed
    }
    focus = {
        id = TST_icon_five
        icon = GFX_zed
    }
}
""",
        encoding="utf-8",
    )
    idx = _indexes(root, cache)
    result = check_refs(
        root,
        files=["common/national_focus/TST_audit.txt"],
        kinds=["duplicate_icons"],
        limit=1,
        **idx,
    )
    assert result["total_duplicate_icons"] == 2
    assert result["counts"]["duplicate_icons"] == {"checked": 5, "unresolved": 2}
    assert result["duplicate_icons"] == [
        {
            "icon": "GFX_shared",
            "focuses": [
                {"id": "TST_icon_one", "file": "common/national_focus/TST_audit.txt", "line": 4},
                {"id": "TST_icon_two", "file": "common/national_focus/TST_audit.txt", "line": 8},
            ],
        }
    ]
    assert result["returned_duplicate_icons"] == 1
    assert result["duplicate_icons_truncated"] is True
    counts_only = check_refs(
        root,
        files=["common/national_focus/TST_audit.txt"],
        kinds=["duplicate_icons"],
        counts_only=True,
        **idx,
    )
    assert "duplicate_icons" not in counts_only
    assert counts_only["total_duplicate_icons"] == 2
    last_page = check_refs(
        root,
        files=["common/national_focus/TST_audit.txt"],
        kinds=["duplicate_icons"],
        limit=1,
        offset=1,
        **idx,
    )
    assert last_page["duplicate_icons_truncated"] is False


def test_duplicate_icons_does_not_group_across_focus_files(audit_mod):
    root, cache = audit_mod
    focus_dir = root / "common" / "national_focus"
    (focus_dir / "AAA.txt").write_text(
        "focus_tree = { focus = { id = AAA_one icon = GFX_shared } }\n",
        encoding="utf-8",
    )
    (focus_dir / "BBB.txt").write_text(
        "focus_tree = { focus = { id = BBB_one icon = GFX_shared } }\n",
        encoding="utf-8",
    )

    result = check_refs(
        root,
        files=["common/national_focus/AAA.txt", "common/national_focus/BBB.txt"],
        kinds=["duplicate_icons"],
        **_indexes(root, cache),
    )

    assert result["duplicate_icons"] == []
    assert result["total_duplicate_icons"] == 0


def test_duplicate_icons_is_opt_in(audit_mod):
    root, cache = audit_mod
    (root / "common" / "national_focus" / "AAA.txt").write_text(
        "focus_tree = {\n"
        "    focus = { id = AAA_one icon = GFX_shared }\n"
        "    focus = { id = AAA_two icon = GFX_shared }\n"
        "}\n",
        encoding="utf-8",
    )

    result = check_refs(root, files=["common/national_focus/AAA.txt"], **_indexes(root, cache))

    assert "duplicate_icons" not in result["kinds_checked"]
    assert "duplicate_icons" not in result["counts"]
    assert "duplicate_icons" not in result
    assert "total_duplicate_icons" not in result


@pytest.mark.integration
def test_duplicate_icons_matches_upstream_script_on_fixture(audit_mod, tmp_path, real_mod_root):
    """The upstream script counts repeated identical icon lines after the first."""
    source_script = real_mod_root / "tools" / "assets" / "duplicate_icon.py"
    root, cache = audit_mod
    source = """focus_tree = {
    focus = {
        id = TST_icon_one
        icon = GFX_shared
    }
    focus = {
        id = TST_icon_two
        icon = GFX_shared
    }
    focus = {
        id = TST_icon_three
        icon = GFX_shared
    }
}
"""
    file_name = "TST_icons.txt"
    (root / "common" / "national_focus" / file_name).write_text(source, encoding="utf-8")
    tools_dir = tmp_path / "tools"
    (tools_dir / "assets").mkdir(parents=True)
    shutil.copyfile(source_script, tools_dir / "assets" / "duplicate_icon.py")
    focus_dir = tmp_path / "common" / "national_focus"
    focus_dir.mkdir(parents=True)
    (focus_dir / file_name).write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "assets/duplicate_icon.py", file_name],
        cwd=tools_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    # The script emits one line for each repeated line after the first. The
    # API groups all focus definitions, so compare its occurrence count with
    # len(group.focuses) - 1 rather than comparing output shape.
    script_count = sum(
        1 for line in proc.stdout.splitlines() if line.strip().lower() == "icon = gfx_shared"
    )
    result = check_refs(
        root,
        files=["common/national_focus/" + file_name],
        kinds=["duplicate_icons"],
        **_indexes(root, cache),
    )
    group = result["duplicate_icons"][0]
    assert group["icon"].casefold() == "gfx_shared"
    assert script_count == len(group["focuses"]) - 1


@pytest.mark.integration
def test_duplicate_icons_matches_upstream_script_on_usa_focus_file(real_mod_root, tmp_path):
    """Normalize script output to duplicate occurrences per icon group."""
    root = real_mod_root
    source = root / "common" / "national_focus" / "05_usa.txt"
    script = root / "tools" / "assets" / "duplicate_icon.py"
    assert source.is_file()
    assert script.is_file()
    proc = subprocess.run(
        [sys.executable, "assets/duplicate_icon.py", source.name],
        cwd=root / "tools",
        check=True,
        capture_output=True,
        text=True,
    )
    script_count = int(proc.stdout.splitlines()[-1].split(" has ")[1].split()[0])
    result = check_refs(
        root,
        files=["common/national_focus/05_usa.txt"],
        kinds=["duplicate_icons"],
        limit=-1,
        **_indexes(root, tmp_path),
    )
    api_count = sum(len(group["focuses"]) - 1 for group in result["duplicate_icons"])
    assert api_count == script_count


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


def test_not_checked_lists_partial_scripted_coverage(audit_mod):
    """Direct scripted calls are only audited when their key is already in the
    index, so we can't flag undefined callers — `scripted_effects` and
    `scripted_triggers` stay in `not_checked` until that changes.
    """
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
    assert "scripted_triggers" in out["not_checked"]


def _write_audit_focus(root: Path, name: str, focus_id: str, event_id: str) -> str:
    path = root / "common" / "national_focus" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""focus_tree = {{
    focus = {{
        id = {focus_id}
        x = 1
        y = 0
        completion_reward = {{ country_event = {event_id} }}
    }}
}}
""",
        encoding="utf-8",
    )
    return f"common/national_focus/{name}"


def test_file_scope_reads_submod_overlay_and_base_fallback(fake_mod_root, cache_dir, tmp_path):
    overlay = tmp_path / "overlay"
    shadowed_rel = _write_audit_focus(
        fake_mod_root, "OVR_shadowed.txt", "OVR_shadowed", "BaseShadow.1"
    )
    _write_audit_focus(overlay, "OVR_shadowed.txt", "OVR_shadowed", "OverlayShadow.1")
    overlay_only_rel = _write_audit_focus(
        overlay, "OVR_overlay_only.txt", "OVR_overlay_only", "OverlayOnly.1"
    )
    base_only_rel = _write_audit_focus(
        fake_mod_root, "OVR_base_only.txt", "OVR_base_only", "BaseOnly.1"
    )

    shadowed = check_refs(
        fake_mod_root,
        files=[shadowed_rel],
        kinds=["event"],
        submod_root=overlay,
        **_indexes(fake_mod_root, cache_dir, submod_root=overlay),
    )
    overlay_only = check_refs(
        fake_mod_root,
        files=[overlay_only_rel],
        kinds=["event"],
        submod_root=overlay,
        **_indexes(fake_mod_root, cache_dir, submod_root=overlay),
    )
    base_only = check_refs(
        fake_mod_root,
        files=[base_only_rel],
        kinds=["event"],
        submod_root=overlay,
        **_indexes(fake_mod_root, cache_dir, submod_root=overlay),
    )

    assert {entry["ref"] for entry in shadowed["unresolved"]} == {"OverlayShadow.1"}
    assert "parse_errors" not in overlay_only
    assert {entry["ref"] for entry in overlay_only["unresolved"]} == {"OverlayOnly.1"}
    assert "parse_errors" not in base_only
    assert {entry["ref"] for entry in base_only["unresolved"]} == {"BaseOnly.1"}


def test_tag_scope_reads_submod_overlay_and_base_fallback(fake_mod_root, cache_dir, tmp_path):
    overlay = tmp_path / "overlay"
    _write_audit_focus(fake_mod_root, "OVR_shadowed.txt", "OVR_shadowed", "BaseShadow.1")
    _write_audit_focus(overlay, "OVR_shadowed.txt", "OVR_shadowed", "OverlayShadow.1")
    _write_audit_focus(overlay, "OVR_overlay_only.txt", "OVR_overlay_only", "OverlayOnly.1")
    _write_audit_focus(fake_mod_root, "OVR_base_only.txt", "OVR_base_only", "BaseOnly.1")

    out = check_refs(
        fake_mod_root,
        tag="OVR",
        kinds=["event"],
        submod_root=overlay,
        **_indexes(fake_mod_root, cache_dir, submod_root=overlay),
    )

    assert out["files_scanned"] == 3
    assert {entry["ref"] for entry in out["unresolved"]} == {
        "OverlayShadow.1",
        "OverlayOnly.1",
        "BaseOnly.1",
    }


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


@pytest.mark.parametrize(
    "value",
    ["ROOT", "FROM", "PREV", "THIS", "OWNER", "CONTROLLER"],
)
def test_is_scope_reference_recognises_keywords(value):
    assert _is_scope_reference(value) is True


@pytest.mark.parametrize(
    "value",
    ["var:foo", "var:prev.tag", "event_target:owner_target", "event_target:vice_king"],
)
def test_is_scope_reference_recognises_dotted_accessors(value):
    assert _is_scope_reference(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "ROOT.FROM",
        "PREV.PREV",
        "FROM.FROM.FROM",
        "ROOT.FROM.FROM.FROM",
        "THIS.owner",
        "THIS.controller",
        "root.from",
    ],
)
def test_is_scope_reference_recognises_scope_chains(value):
    assert _is_scope_reference(value) is True


def test_is_scope_reference_keeps_bare_lowercase_scope_names_as_tags():
    assert _is_scope_reference("owner") is False


@pytest.mark.parametrize(
    "value",
    ["ROOT.FOO", "ROOT.", ".FROM", "ROOT..FROM", "USA.FROM", "ROOT.original_tag"],
)
def test_is_scope_reference_rejects_malformed_chains(value):
    assert _is_scope_reference(value) is False


@pytest.mark.parametrize(
    "value",
    ["USA", "GER", "SOV", "TST", "TAG", "USA_cosmetic_tag_monarchist", "GER_fourth_reich"],
)
def test_is_scope_reference_keeps_real_tag_names(value):
    assert _is_scope_reference(value) is False


def test_country_tag_audit_skips_scope_keywords_and_dotted_refs(fake_mod_root, cache_dir):
    """`tag = ROOT` and `original_tag = var:prev.tag` aren't references to
    indexed tags; they must not show up as unresolved dangling refs.
    """
    body = """focus_tree = {
    focus = {
        id = TST_scope_root
        x = 1
        y = 0
        available = {
            tag = ROOT
            tag = FROM
            tag = { original_tag = var:prev.original_tag }
            original_tag = event_target:aggressor
        }
    }
}
"""
    f = fake_mod_root / "common" / "national_focus" / "TST_scope_root.txt"
    f.write_text(body, encoding="utf-8")
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_scope_root.txt"],
        kinds=["country_tag"],
        country_tag_index=CountryTagIndex(fake_mod_root, cache_dir, include_vanilla=False),
        **_indexes(fake_mod_root, cache_dir),
    )
    unresolved = {(e["kind"], e["ref"]) for e in out["unresolved"]}
    assert ("country_tag", "ROOT") not in unresolved
    assert ("country_tag", "FROM") not in unresolved
    assert ("country_tag", "var:prev.original_tag") not in unresolved
    assert ("country_tag", "event_target:aggressor") not in unresolved
    assert out["counts"]["country_tag"]["checked"] == 0


def test_country_tag_audit_skips_scope_chains(fake_mod_root, cache_dir):
    """`FROM = { tag = ROOT.FROM }` (peace conference AI) is a scope chain; a
    malformed chain and an unknown literal tag in the same file still report.
    """
    body = """GENERIC_wants_its_cores = {
    enable = {
        tag = ROOT
        FROM = { tag = ROOT.FROM }
        FROM = { tag = ROOT.FOO }
        NOT = { tag = PREV.PREV.PREV }
        original_tag = TST_GHOST_TAG
    }
}
"""
    f = fake_mod_root / "common" / "national_focus" / "TST_scope_chain.txt"
    f.write_text(body, encoding="utf-8")
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_scope_chain.txt"],
        kinds=["country_tag"],
        country_tag_index=CountryTagIndex(fake_mod_root, cache_dir, include_vanilla=False),
        **_indexes(fake_mod_root, cache_dir),
    )
    unresolved = {(e["kind"], e["ref"]) for e in out["unresolved"]}
    assert unresolved == {("country_tag", "ROOT.FOO"), ("country_tag", "TST_GHOST_TAG")}
    assert out["counts"]["country_tag"]["checked"] == 2


def test_country_tag_audit_drops_set_cosmetic_tag(fake_mod_root, cache_dir):
    """`set_cosmetic_tag` carries a cosmetic-tag *name*, not a country tag —
    auditing it against the country-tag index would always be unresolved.
    """
    body = """focus_tree = {
    focus = {
        id = TST_cosmetic
        x = 1
        y = 0
        completion_reward = {
            set_cosmetic_tag = TST_cosmetic_monarchist
        }
    }
}
"""
    f = fake_mod_root / "common" / "national_focus" / "TST_cosmetic.txt"
    f.write_text(body, encoding="utf-8")
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_cosmetic.txt"],
        kinds=["country_tag"],
        country_tag_index=CountryTagIndex(fake_mod_root, cache_dir, include_vanilla=False),
        **_indexes(fake_mod_root, cache_dir),
    )
    assert out["counts"]["country_tag"]["checked"] == 0
    assert out["total_unresolved"] == 0


def test_scope_exclusions_only_apply_to_country_tags(fake_mod_root, cache_dir):
    source = fake_mod_root / "events" / "scope_names.txt"
    source.write_text("original_tag = TAG\nhas_character = ROOT\nhas_trait = FROM\n")
    out = check_refs(
        fake_mod_root,
        files=["events/scope_names.txt"],
        kinds=["country_tag", "character", "trait"],
        **_indexes(fake_mod_root, cache_dir),
    )

    assert {(entry["kind"], entry["ref"]) for entry in out["unresolved"]} == {
        ("country_tag", "TAG"),
        ("character", "ROOT"),
        ("trait", "FROM"),
    }


def test_country_tag_audit_still_flags_unknown_tag(fake_mod_root, cache_dir):
    """A real-looking but-unindexed tag id still shows up as unresolved."""
    body = """focus_tree = {
    focus = {
        id = TST_real_unknown
        x = 1
        y = 0
        available = {
            original_tag = TST_GHOST_TAG
        }
    }
}
"""
    f = fake_mod_root / "common" / "national_focus" / "TST_real_unknown.txt"
    f.write_text(body, encoding="utf-8")
    out = check_refs(
        fake_mod_root,
        files=["common/national_focus/TST_real_unknown.txt"],
        kinds=["country_tag"],
        country_tag_index=CountryTagIndex(fake_mod_root, cache_dir, include_vanilla=False),
        **_indexes(fake_mod_root, cache_dir),
    )
    unresolved = {(e["kind"], e["ref"]) for e in out["unresolved"]}
    assert ("country_tag", "TST_GHOST_TAG") in unresolved


def test_block_valued_reference_nodes_are_read_through_their_inner_key(fake_mod_root, cache_dir):
    source = fake_mod_root / "events" / "block_refs.txt"
    source.write_text(
        "icon = { value = GFX_missing_block_icon }\n"
        "modify_character = { character = TST_missing_character }\n",
        encoding="utf-8",
    )

    out = check_refs(
        fake_mod_root,
        files=["events/block_refs.txt"],
        kinds=["sprite", "character"],
        **_indexes(fake_mod_root, cache_dir),
    )

    assert {(entry["kind"], entry["ref"]) for entry in out["unresolved"]} == {
        ("sprite", "GFX_missing_block_icon"),
        ("character", "TST_missing_character"),
    }


# ----- country tag, character, trait, and scripted definition kinds ------------

_DEFINITION_REFS = """focus_tree = {
    focus = {
        id = TST_new_refs
        original_tag = TST
        character = TST_test_character
        add_trait = TST_test_trait
        TST_test_effect = {}
        TST_test_trigger = {}
    }
}
"""
_SCRIPTED_KINDS = ["scripted_effect", "scripted_trigger"]


def _audit_definitions(root: Path, cache: Path, text: str, kinds: list[str]) -> dict:
    (root / "common" / "national_focus" / "TST_new_refs.txt").write_text(text, encoding="utf-8")
    return check_refs(
        root,
        files=["common/national_focus/TST_new_refs.txt"],
        kinds=kinds,
        country_tag_index=CountryTagIndex(root, cache, include_vanilla=False),
        character_index=CharacterIndex(root, cache, include_vanilla=False),
        trait_index=TraitIndex(root, cache, include_vanilla=False),
        scripted_effect_index=ScriptedEffectIndex(root, cache, include_vanilla=False),
        scripted_trigger_index=ScriptedTriggerIndex(root, cache, include_vanilla=False),
        **_indexes(root, cache),
    )


def test_check_refs_resolves_new_definition_kinds(fake_mod_root, cache_dir):
    result = _audit_definitions(
        fake_mod_root,
        cache_dir,
        _DEFINITION_REFS,
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
    result = _audit_definitions(fake_mod_root, cache_dir, text, _SCRIPTED_KINDS)

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
    result = _audit_definitions(fake_mod_root, cache_dir, body, _SCRIPTED_KINDS)

    assert result["total_unresolved"] == 0
    # Neither fake wrapper contributes a checked reference.
    assert result["counts"]["scripted_effect"]["checked"] == 0
    assert result["counts"]["scripted_trigger"]["checked"] == 0


# ----- incomplete indexes --------------------------------------------------------

_AUDIT_FILES = ["common/national_focus/TST_audit.txt"]


def _break(root: Path, subdir: str, name: str, text: str) -> str:
    (root / subdir / name).write_text(text, encoding="utf-8")
    return str(Path(subdir) / name)


def test_check_refs_reports_bounded_index_errors_per_kind(audit_mod):
    root, cache = audit_mod
    for n in range(7):
        _break(root, "events", f"broken_{n}.txt", "country_event = {{{")
    idea = _break(root, "common/ideas", "broken.txt", "ideas = {{{")
    _break(root, "interface", "broken.gfx", "spriteTypes = { spriteType = {{{")
    _break(root, "common/national_focus", "broken.txt", "focus_tree = { focus = { id = X x = {{{")

    out = check_refs(root, files=_AUDIT_FILES, **_indexes(root, cache))

    errors = out["index_errors"]
    assert set(errors) == {"focus", "event", "idea", "sprite"}
    assert errors["event"]["total"] == 7
    assert errors["event"]["truncated"] is True
    assert len(errors["event"]["sample"]) == 5
    for entry in errors["event"]["sample"]:
        assert set(entry) == {"file", "error"}
        assert entry["error"].startswith("parse failed:")
    assert errors["idea"] == {
        "total": 1,
        "sample": [{"file": idea, "error": errors["idea"]["sample"][0]["error"]}],
        "truncated": False,
    }
    assert errors["idea"]["sample"][0]["error"].startswith("parse failed:")


def test_check_refs_marks_incomplete_kinds_without_hiding_the_misses(audit_mod):
    root, cache = audit_mod
    _break(root, "events", "broken.txt", "country_event = {{{")

    out = check_refs(root, files=_AUDIT_FILES, **_indexes(root, cache))

    assert {kind for kind, count in out["counts"].items() if count.get("incomplete")} == {"event"}
    assert ("event", "Missing.99") in {(e["kind"], e["ref"]) for e in out["unresolved"]}
    assert out["counts"]["event"]["unresolved"] == 1


def test_check_refs_only_reports_selected_kinds(audit_mod):
    root, cache = audit_mod
    _break(root, "events", "broken.txt", "country_event = {{{")
    _break(root, "common/ideas", "broken.txt", "ideas = {{{")

    out = check_refs(root, files=_AUDIT_FILES, kinds=["event"], **_indexes(root, cache))

    assert set(out["index_errors"]) == {"event"}


def test_check_refs_omits_index_errors_when_every_index_is_clean(audit_mod):
    root, cache = audit_mod

    out = check_refs(root, files=_AUDIT_FILES, **_indexes(root, cache))

    assert "index_errors" not in out
    assert all("incomplete" not in count for count in out["counts"].values())


def test_check_refs_reports_an_incomplete_scripted_index(fake_mod_root, cache_dir):
    _break(fake_mod_root, "common/scripted_effects", "broken.txt", "TST_broken = {{{")

    result = _audit_definitions(fake_mod_root, cache_dir, _DEFINITION_REFS, _SCRIPTED_KINDS)

    assert set(result["index_errors"]) == {"scripted_effect"}
    assert result["index_errors"]["scripted_effect"]["total"] == 1
    assert result["counts"]["scripted_effect"]["incomplete"] is True
    assert "incomplete" not in result["counts"]["scripted_trigger"]
