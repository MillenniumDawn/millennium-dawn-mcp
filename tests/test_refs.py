"""find_references analysis tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from md_mcp.analysis.refs import (
    Kind,
    _character_pattern,
    _country_tag_pattern,
    _decision_pattern,
    _event_pattern,
    _flag_pattern,
    _focus_pattern,
    _idea_pattern,
    _loc_pattern,
    _named_definition_pattern,
    _sprite_pattern,
    _trait_pattern,
    _variable_pattern,
    find_references,
)
from md_mcp.paradox.schema import EVENT_KINDS


def test_event_pattern_matches_every_canonical_event_kind():
    assert any(EVENT_KINDS)
    pattern = _event_pattern("TST.1")
    for kind in EVENT_KINDS:
        assert pattern.search(f"{kind} = TST.1") is not None
        assert pattern.search(f"{kind} = {{ id = TST.1 title = title }}") is not None
        assert pattern.search(f"my_{kind} = TST.1") is None
        assert pattern.search(f"{kind}_extra = TST.1") is None
        assert pattern.search(f"{kind} = TST.12") is None
        assert pattern.search(f"{kind} = {{ id = TST.12 }}") is None


def test_variable_pattern_matches_explicit_short_and_scoped_references():
    pattern = _variable_pattern("temp_opinion")
    assert pattern.search("set_temp_variable = { temp_opinion = 10 }") is not None
    assert pattern.search("check_variable = { var = ROOT.temp_opinion }") is not None
    assert pattern.search("check_variable = { var = ROOT.temp_opinion_extra }") is None
    assert pattern.search('check_variable = { var = "ROOT.temp_opinion" }') is not None
    assert pattern.search("has_variable = THIS.temp_opinion") is not None
    scoped_pattern = _variable_pattern("ROOT.temp_opinion")
    assert scoped_pattern.search("check_variable = { var = ROOT.temp_opinion }") is not None
    assert scoped_pattern.search('check_variable = { var = "ROOT.temp_opinion" }') is not None
    assert scoped_pattern.search('check_variable = { var = "THIS.temp_opinion" }') is None
    assert pattern.search("var = temp_opinion_extra") is None


def test_focus_and_idea_patterns_require_complete_dotted_identifiers():
    assert _focus_pattern("TST.root").search("focus = TST.root.extra") is None
    assert _focus_pattern("TST.root").search("focus = TST.root") is not None
    assert _idea_pattern("TST.idea").search("has_idea = TST.idea.extra") is None
    assert _idea_pattern("TST.idea").search("has_idea = TST.idea") is not None


@pytest.mark.parametrize(
    ("pattern_factory", "reference"),
    [
        (_decision_pattern, "decision = TST.id.extra"),
        (_flag_pattern, "has_country_flag = TST.id.extra"),
        (_country_tag_pattern, "tag = TST.id.extra"),
        (_character_pattern, "character = TST.id.extra"),
        (_trait_pattern, "add_trait = TST.id.extra"),
        (_loc_pattern, "TST.id.extra"),
        (_sprite_pattern, "GFX_TST.id.extra"),
        (_named_definition_pattern, "TST.id.extra = {}"),
    ],
)
def test_reference_patterns_require_complete_dotted_identifiers(pattern_factory, reference):
    target = "GFX_TST.id" if pattern_factory is _sprite_pattern else "TST.id"
    pattern = pattern_factory(target)
    assert pattern.search(reference) is None


def test_event_pattern_requires_complete_dotted_identifier():
    pattern = _event_pattern("TST.1")
    assert pattern.search("country_event = TST.1.extra") is None
    assert pattern.search("country_event = TST.1") is not None


def test_find_variable_references_ignores_comments_and_quoted_prose(fake_mod_root):
    path = fake_mod_root / "common" / "scripted_effects" / "variable_refs.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# set_temp_variable = { temp_opinion = 10 }\n"
        'custom_tooltip = "set_temp_variable = { temp_opinion = 10 }"\n'
        'custom_tooltip = "prose with an escaped \\" quote\\n'
        'set_temp_variable = { temp_opinion = 10 }\nstill quoted prose"\n'
        "set_temp_variable = {\n    temp_opinion = 10\n}\n"
        'var = "temp_opinion"\n',
        encoding="utf-8",
    )

    result = find_references(fake_mod_root, "variable", "temp_opinion")

    assert result["total"] == 2
    assert [match["line"] for match in result["matches"]] == [5, 8]
    assert [match["col"] for match in result["matches"]] == [1, 1]
    assert find_references(fake_mod_root, "variable", "temp_opinion", limit=1, offset=1)[
        "matches"
    ] == [result["matches"][1]]


@pytest.mark.parametrize(
    ("kind", "target", "text", "expected"),
    [
        ("event", "TST_block.1", "country_event = {\n\t# fires later\n\tid = TST_block.1\n}\n", 1),
        ("event", "TST_block.1", "country_event = {\n\t# id = TST_block.1\n}\n", 0),
        ("idea", "TST_block_idea", "add_ideas = {\n\t# comment\n\tidea = TST_block_idea\n}\n", 1),
        ("variable", "block_var", "set_temp_variable = {\n\t# note\n\tblock_var = 10\n}\n", 1),
        (
            "variable",
            "block_var",
            'check_variable = {\n\ttooltip = "x"\n\tvar = block_var value = 1\n}\n',
            1,
        ),
    ],
    ids=[
        "event-comment",
        "event-commented-target",
        "idea-comment",
        "variable-comment",
        "variable-string",
    ],
)
def test_find_references_judges_the_target_not_tokens_before_it(
    fake_mod_root, kind, target, text, expected
):
    path = fake_mod_root / "events" / "block_refs.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    assert find_references(fake_mod_root, kind, target)["total"] == expected


def test_find_variable_references_match_quoted_scoped_values_only_in_code(fake_mod_root):
    path = fake_mod_root / "common" / "scripted_effects" / "quoted_scoped_refs.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '# check_variable = { var = "ROOT.temp_opinion" }\n'
        'custom_tooltip = "check_variable = { var = \\"ROOT.temp_opinion\\" }"\n'
        'check_variable = { var = "ROOT.temp_opinion" }\n'
        'check_variable = { var = "THIS.temp_opinion" }\n'
        'check_variable = { var = "ROOT.temp_opinion_extra" }\n',
        encoding="utf-8",
    )

    base_matches = find_references(fake_mod_root, "variable", "temp_opinion")
    root_matches = find_references(fake_mod_root, "variable", "ROOT.temp_opinion")
    this_matches = find_references(fake_mod_root, "variable", "THIS.temp_opinion")

    assert base_matches["total"] == 2
    assert root_matches["total"] == 1
    assert this_matches["total"] == 1
    assert [match["line"] for match in root_matches["matches"]] == [3]


def test_loc_references_keep_marked_localisation_text_but_ignore_comments_and_prose(fake_mod_root):
    loc_path = fake_mod_root / "localisation" / "english" / "loc_l_english.yml"
    loc_path.parent.mkdir(parents=True, exist_ok=True)
    target = "TST_loc_marker_unique"
    loc_path.write_text(
        f'FOO:0 "text ${target}$, plain mention {target}, marker [{target}]" # {target}\n'
        f'BAR:0 "quoted prose mentioning {target} without a reference marker"\n',
        encoding="utf-8",
    )
    script_path = fake_mod_root / "common" / "scripted_effects" / "loc_refs.txt"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        f'# custom_effect_tooltip = "{target}"\n'
        f'custom_effect_tooltip = "{target}"\n'
        f'custom_effect_tooltip = "quoted prose mentioning {target}"\n',
        encoding="utf-8",
    )

    result = find_references(fake_mod_root, "loc", target)

    assert result["total"] == 3
    assert {match["file"] for match in result["matches"]} == {
        "localisation/english/loc_l_english.yml",
        "common/scripted_effects/loc_refs.txt",
    }
    assert [match["line"] for match in result["matches"]] == [1, 1, 2]


def test_find_references_keeps_quoted_gui_sprite_values(fake_mod_root):
    path = fake_mod_root / "interface" / "sprites.gui"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '# spriteType = { name = "GFX_test_icon" }\n'
        'spriteType = { name = "GFX_test_icon" texturefile = "gfx/test.dds" }\n'
        'buttonType = { name = "other" }\n',
        encoding="utf-8",
    )

    result = find_references(fake_mod_root, "sprite", "GFX_test_icon")

    assert result["total"] == 1
    assert result["matches"][0]["line"] == 2
    assert result == find_references(fake_mod_root, "sprite", "GFX_test_icon", prefilter=False)


def test_find_focus_references_in_focus_file(fake_mod_root):
    # TST_branch_a has prerequisite = { focus = TST_root }, so 'focus = TST_root' appears
    r = find_references(fake_mod_root, "focus", "TST_root")
    assert r["ok"]
    assert r["total"] >= 2  # TST_branch_a and TST_branch_b both prereq TST_root
    files = {m["file"] for m in r["matches"]}
    assert any(f.endswith("test.txt") for f in files)


def test_find_idea_references(fake_mod_root):
    # TST_root has `available = { has_idea = TST_starter }`
    r = find_references(fake_mod_root, "idea", "TST_starter")
    assert r["ok"]
    assert r["total"] >= 1
    assert any("has_idea" in m["snippet"] for m in r["matches"])


def test_find_loc_references_returns_definitions_and_uses(fake_mod_root):
    # TST_root is defined in the loc file and used as a focus id (which appears in the focus file).
    r = find_references(fake_mod_root, "loc", "TST_root")
    assert r["ok"]
    files = {m["file"] for m in r["matches"]}
    # Should find both the loc definition (.yml) and the focus id reference (.txt).
    assert any(f.endswith(".yml") for f in files)
    assert any(f.endswith(".txt") for f in files)


def test_find_references_unknown_kind(fake_mod_root):
    r = find_references(fake_mod_root, cast(Kind, "bogus"), "x")
    assert r["ok"] is False
    assert "kind" in r["error"]


def test_find_references_respects_limit(fake_mod_root):
    r = find_references(fake_mod_root, "focus", "TST_root", limit=1)
    assert r["returned"] == 1
    assert r["truncated"] is True
    assert r["total"] >= 2


def test_find_references_offset_paginates(fake_mod_root):
    """`offset` skips ahead in the match list."""
    page_one = find_references(fake_mod_root, "focus", "TST_root", limit=1, offset=0)
    page_two = find_references(fake_mod_root, "focus", "TST_root", limit=1, offset=1)
    assert page_one["matches"][0] != page_two["matches"][0]
    # Both pages report the same total.
    assert page_one["total"] == page_two["total"]


def test_find_references_files_only_mode(fake_mod_root):
    """`files_only=True` collapses to a unique file list with hit counts."""
    r = find_references(fake_mod_root, "loc", "TST_root", files_only=True)
    assert r["ok"]
    assert r["mode"] == "files_only"
    assert "files" in r
    for entry in r["files"]:
        assert "file" in entry and "hits" in entry
        assert entry["hits"] >= 1


def test_find_references_snippet_chars_clip(fake_mod_root):
    """`snippet_chars` clips the per-match line snippet."""
    r = find_references(fake_mod_root, "focus", "TST_root", snippet_chars=5)
    for m in r["matches"]:
        assert len(m["snippet"]) <= 5


def test_find_references_scans_submod_then_vanilla_roots(fake_mod_root, tmp_path):
    submod = tmp_path / "Overlay"
    vanilla = tmp_path / "Vanilla"
    for root, name, filename in (
        (submod, "submod", "overlay.txt"),
        (vanilla, "vanilla", "vanilla.txt"),
    ):
        focus_dir = root / "common" / "national_focus"
        focus_dir.mkdir(parents=True)
        (focus_dir / filename).write_text(f"focus = TST_{name}_ref\n", encoding="utf-8")

    vanilla_hit = find_references(
        fake_mod_root,
        "focus",
        "TST_vanilla_ref",
        include_vanilla=True,
        vanilla_path=vanilla,
        submod_root=submod,
    )
    assert vanilla_hit["ok"]
    assert vanilla_hit["total"] == 1
    assert vanilla_hit["matches"][0]["file"] == "common/national_focus/vanilla.txt"
    assert "TST_vanilla_ref" in vanilla_hit["matches"][0]["snippet"]

    without_vanilla = find_references(
        fake_mod_root,
        "focus",
        "TST_vanilla_ref",
        vanilla_path=vanilla,
        submod_root=submod,
    )
    assert without_vanilla["total"] == 0


def test_find_references_skips_files_shadowed_by_higher_priority_root(fake_mod_root, tmp_path):
    submod = tmp_path / "Overlay"
    vanilla = tmp_path / "Vanilla"
    rel = Path("common/national_focus/shadow.txt")
    for root, text in (
        (fake_mod_root, "focus = TST_shadow_ref\n"),
        (submod, "focus = TST_other\n"),
        (vanilla, "focus = TST_shadow_ref\n"),
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")

    for files_only in (False, True):
        result = find_references(
            fake_mod_root,
            "focus",
            "TST_shadow_ref",
            include_vanilla=True,
            vanilla_path=vanilla,
            submod_root=submod,
            files_only=files_only,
        )
        assert result["ok"]
        assert result.get("total", result.get("total_files")) == 0


def test_find_references_files_only_does_not_merge_shadowed_counts(fake_mod_root, tmp_path):
    submod = tmp_path / "Overlay"
    rel = Path("common/national_focus/shadow.txt")
    for root, text in (
        (fake_mod_root, "focus = TST_dup\n"),
        (submod, "focus = TST_dup\nfocus = TST_dup\n"),
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")

    result = find_references(fake_mod_root, "focus", "TST_dup", submod_root=submod, files_only=True)

    assert result["files"] == [{"file": str(rel), "hits": 2}]


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("country_tag", "TST"),
        ("character", "TST_test_character"),
        ("trait", "TST_test_trait"),
        ("scripted_effect", "TST_test_effect"),
        ("scripted_trigger", "TST_test_trigger"),
    ],
)
def test_find_references_supports_definition_kinds(fake_mod_root, kind, target):
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

    result = find_references(fake_mod_root, kind, target)

    assert result["ok"]
    assert result["total"] >= 1


def test_find_references_stops_scanning_at_the_cap(fake_mod_root, tmp_path):
    # The cap is fifty times the page, and never under 5000 matches.
    submod = tmp_path / "Overlay"
    hot = submod / "common" / "national_focus" / "hot.txt"
    hot.parent.mkdir(parents=True)
    hot.write_text("focus = TST_hot\n" * 5001, encoding="utf-8")
    (fake_mod_root / "common" / "national_focus" / "more.txt").write_text(
        "focus = TST_hot\n", encoding="utf-8"
    )

    result = find_references(fake_mod_root, "focus", "TST_hot", submod_root=submod)

    assert result["scan_truncated"] is True
    assert result["total"] == 5000
    assert result["truncated"] is True


def test_find_references_skips_a_directory_named_like_a_script(fake_mod_root):
    (fake_mod_root / "common" / "national_focus" / "folder.txt").mkdir()
    (fake_mod_root / "common" / "national_focus" / "last_line.txt").write_text(
        "focus = TST_last_line", encoding="utf-8"
    )

    result = find_references(fake_mod_root, "focus", "TST_last_line")

    # The match is on a final line with no newline after it.
    assert [m["snippet"] for m in result["matches"]] == ["focus = TST_last_line"]
