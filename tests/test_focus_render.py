"""Tests for render_focus_tree: tree choice, offsets, crops, files written, and errors."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from md_mcp.analysis import focus_render
from md_mcp.analysis.focus_render import (
    EVALUATOR,
    _font,
    _label_lines,
    _label_room,
    _segments,
    _wrap,
    render_focus_tree,
)
from md_mcp.indexes import FocusIndex, GfxIndex, LocalisationIndex
from md_mcp.util.response import BUDGET_BYTES
from tests.support.mod import git, init_repo, make_mod_root
from tests.support.upstream import plant_focus_scenario

pytestmark = pytest.mark.usefixtures("fresh_scenario_imports")

_TREE = """focus_tree = {
    id = rnd_tree
    country = { factor = 0 modifier = { add = 10 tag = RND } }
    shared_focus = SHR_root
    focus = {
        id = RND_root
        icon = GFX_rnd_icon
        x = 4
        y = 0
    }
    focus = {
        id = RND_left
        icon = GFX_rnd_icon
        x = -2
        y = 1
        relative_position_id = RND_root
        prerequisite = { focus = RND_root }
        mutually_exclusive = { focus = RND_right }
    }
    focus = {
        id = RND_right
        icon = GFX_rnd_missing
        x = 2
        y = 1
        relative_position_id = RND_root
        prerequisite = { focus = RND_root }
        mutually_exclusive = { focus = RND_left }
    }
    focus = {
        id = RND_end
        x = 0
        y = 2
        relative_position_id = RND_root
        prerequisite = { focus = RND_left focus = RND_right }
    }
}
"""

_SHARED = """shared_focus = {
    id = SHR_root
    icon = GFX_rnd_icon
    x = 10
    y = 0
    offset = {
        x = 5
        y = 1
        trigger = { tag = RND }
    }
    offset = {
        x = 50
        y = 0
        trigger = { has_country_flag = unknown_flag }
    }
}
shared_focus = {
    id = SHR_child
    x = 0
    y = 1
    relative_position_id = SHR_root
    prerequisite = { focus = SHR_root }
}
"""

_OTHER = """focus_tree = {
    id = other_tree
    country = { factor = 0 modifier = { add = 10 tag = OTH } }
    shared_focus = SHR_root
    focus = {
        id = OTH_root
        x = 0
        y = 0
    }
}
"""

_GFX = """spriteTypes = {
    spriteType = {
        name = "GFX_rnd_icon"
        texturefile = "gfx/interface/goals/rnd_icon.tga"
    }
    spriteType = {
        name = "GFX_rnd_missing"
        texturefile = "gfx/interface/goals/not_there.dds"
    }
}
"""

_LOC = '﻿l_english:\n RND_root:0 "§YThe Root§!"\n RND_left:0 "Left Way"\n'


@pytest.fixture
def mod(tmp_path: Path) -> Path:
    root = make_mod_root(tmp_path / "Mod")
    focus_dir = root / "common" / "national_focus"
    focus_dir.mkdir(parents=True)
    (focus_dir / "rnd.txt").write_text(_TREE, encoding="utf-8")
    (focus_dir / "shr_shared.txt").write_text(_SHARED, encoding="utf-8")
    (focus_dir / "other.txt").write_text(_OTHER, encoding="utf-8")
    (root / "interface").mkdir()
    (root / "interface" / "rnd.gfx").write_text(_GFX, encoding="utf-8")
    loc = root / "localisation" / "english"
    loc.mkdir(parents=True)
    (loc / "rnd_l_english.yml").write_text(_LOC, encoding="utf-8")
    pil = pytest.importorskip("PIL.Image")
    goals = root / "gfx" / "interface" / "goals"
    goals.mkdir(parents=True)
    pil.new("RGBA", (40, 30), (200, 50, 50, 255)).save(goals / "rnd_icon.tga")
    plant_focus_scenario(root)
    return root


@pytest.fixture(params=["stand-in", pytest.param("mod", marks=pytest.mark.integration)])
def scenario_mod(request: pytest.FixtureRequest, mod: Path) -> Path:
    """`mod`, with the evaluator stand-ins or, under integration, the mod's real scripts."""
    if request.param == "mod":
        plant_focus_scenario(mod, request.getfixturevalue("real_mod_root"))
    return mod


def _render(mod: Path, cache: Path, **kwargs) -> dict:
    return render_focus_tree(
        mod,
        cache,
        FocusIndex(mod, cache),
        GfxIndex(mod, cache),
        LocalisationIndex(mod, cache),
        **kwargs,
    )


def test_png_and_html_are_written_under_the_cache(mod: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    result = _render(mod, cache, tag="RND", format="both")

    assert result["ok"] is True
    assert result["tree"] == {"id": "rnd_tree", "file": "common/national_focus/rnd.txt", "line": 1}
    assert result["country"] == "RND"
    assert result["country_source"] == "tree"
    png = Path(result["outputs"]["png"])
    page = Path(result["outputs"]["html"])
    assert png.parent == cache / "renders" and png.is_file()
    assert page.parent == cache / "renders" and page.is_file()
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert (
        result["image"]["width"] == (result["region"]["max_x"] - result["region"]["min_x"] + 1) * 96
    )
    assert result["icons"]["resolved"] == 1
    assert result["icons"]["missing_sample"] == ["GFX_rnd_missing"]
    assert result["icons"]["no_icon"] >= 1
    text = page.read_text(encoding="utf-8")
    assert "The Root" in text and "§" not in text
    assert "data:image/png;base64," in text
    assert "1px dashed #88aaff" in text and "#ff0000" in text
    assert not any(path.name.startswith("rnd") for path in mod.rglob("*.png"))


def test_tag_offset_applies_only_when_its_trigger_holds(mod: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    own = _render(mod, cache, tag="RND", format="html", focus_ids=["SHR_root"], margin=0)
    other = _render(mod, cache, tree="other_tree", format="html", focus_ids=["SHR_root"], margin=0)

    assert own["region"] == {"min_x": 15, "min_y": 1, "max_x": 15, "max_y": 1}
    assert other["country"] == "OTH"
    assert other["region"] == {"min_x": 10, "min_y": 0, "max_x": 10, "max_y": 0}
    assert own["offsets"]["applied"] == 1
    assert own["offsets"]["unevaluated"] == 1
    assert own["offsets"]["unevaluated_sample"] == [{"focus": "SHR_root", "x": 50.0, "y": 0.0}]


def test_country_argument_overrides_the_tree(mod: Path, tmp_path: Path) -> None:
    result = _render(
        mod, tmp_path / "cache", tag="RND", country="XXX", format="html", focus_ids=["SHR_root"]
    )

    assert result["country"] == "XXX" and result["country_source"] == "argument"
    assert result["offsets"]["applied"] == 0


def test_hide_branches_and_highlight(mod: Path, tmp_path: Path) -> None:
    result = _render(
        mod,
        tmp_path / "cache",
        tag="RND",
        format="html",
        hide_branches=["SHR_*"],
        highlight=["RND_l*", "NOPE_*"],
    )

    assert result["focuses"]["hidden"] == 2
    assert result["focuses"]["highlighted"] == 1
    assert result["warnings"] == ["highlight: NOPE_* matched no focus in the tree"]
    page = Path(result["outputs"]["html"]).read_text(encoding="utf-8")
    assert "SHR_child" not in page
    assert 'class="f hl"' in page


def test_hiding_one_or_branch_keeps_the_child(mod: Path, tmp_path: Path) -> None:
    result = _render(mod, tmp_path / "cache", tag="RND", format="html", hide_branches=["RND_left"])

    page = Path(result["outputs"]["html"]).read_text(encoding="utf-8")
    assert result["focuses"]["hidden"] == 1
    assert "RND_end" in page


def test_region_crop_limits_the_rendered_focuses(mod: Path, tmp_path: Path) -> None:
    result = _render(mod, tmp_path / "cache", tag="RND", format="html", region=[2, 0, 6, 1])

    assert result["region"] == {"min_x": 2, "min_y": 0, "max_x": 6, "max_y": 1}
    assert result["focuses"]["rendered"] == 3


def test_ref_renders_a_committed_revision(mod: Path, tmp_path: Path) -> None:
    init_repo(mod)
    git(mod, "add", "-A")
    git(mod, "commit", "-qm", "tree")
    tree_file = mod / "common" / "national_focus" / "rnd.txt"
    tree_file.write_text(_TREE.replace("x = 4", "x = 9"), encoding="utf-8")
    cache = tmp_path / "cache"

    before = _render(
        mod, cache, tag="RND", format="html", focus_ids=["RND_root"], margin=0, ref="HEAD"
    )
    after = _render(mod, cache, tag="RND", format="html", focus_ids=["RND_root"], margin=0)

    assert before["source"] == "HEAD" and before["region"]["min_x"] == 4
    assert after["source"] == "working tree" and after["region"]["min_x"] == 9
    assert before["outputs"]["html"] != after["outputs"]["html"]


def test_unknown_ref_reports_the_git_error(mod: Path, tmp_path: Path) -> None:
    init_repo(mod)
    git(mod, "add", "-A")
    git(mod, "commit", "-qm", "tree")

    result = _render(mod, tmp_path / "cache", tag="RND", format="html", ref="no-such-branch")

    assert result["ok"] is False
    assert result["error"] == "Could not read or parse the focus files for this scope."
    assert result["parse_errors"][0]["file"] == "common/national_focus/rnd.txt"
    assert result["parse_errors"][0]["error"]


def test_shared_only_tag_points_at_importing_trees(mod: Path, tmp_path: Path) -> None:
    result = _render(mod, tmp_path / "cache", tag="SHR", format="html", limit=1)

    assert result["ok"] is False
    assert result["shared_roots"] == ["SHR_root"]
    assert result["trees_total"] == 2
    assert result["trees_truncated"] is True
    assert len(result["trees"]) == 1


def test_shared_file_points_at_importing_trees(mod: Path, tmp_path: Path) -> None:
    result = _render(mod, tmp_path / "cache", file="common/national_focus/shr_shared.txt")

    assert result["ok"] is False
    assert {"file": "common/national_focus/rnd.txt"} in result["trees"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "Pass tree="),
        ({"tag": "RND", "format": "gif"}, "format must be one of"),
        ({"tag": "RND", "labels": "all"}, "labels must be one of"),
        ({"tag": "RND", "region": [3, 0, 1, 1]}, "min_x <= max_x"),
        ({"tag": "RND", "scale": 5}, "scale must be between"),
        ({"tag": "RND", "ref": "--output=x"}, "ref must be a plain git revision"),
        ({"tag": "RND", "country": "TOOLONG"}, "country must be"),
        ({"tag": "RND", "focus_ids": "RND_root"}, "focus_ids must be a list"),
        ({"tag": "RND", "date": "June 2010"}, "date must be a YYYY.M.D string"),
        ({"tag": "RND", "date": "2010.13.45"}, "date must be a valid calendar date"),
        ({"tag": "RND", "date": "2010.2.30"}, "date must be a valid calendar date"),
        ({"tag": "RND", "completed": "RND_root"}, "completed must be a list"),
        ({"tag": "RND", "completed": ["X"] * 201}, "completed must be a list of at most 200"),
        ({"tree": "no_such_tree"}, "No focus file matches"),
        ({"tag": "RND", "focus_ids": ["ZZZ_*"]}, "focus_ids matched no visible focus"),
    ],
)
def test_bad_arguments_return_errors(mod: Path, tmp_path: Path, kwargs: dict, message: str) -> None:
    result = _render(mod, tmp_path / "cache", **kwargs)

    assert result["ok"] is False
    assert message in result["error"]


def test_png_without_pillow_asks_for_html(
    mod: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(focus_render, "_pillow", lambda: None)

    png = _render(mod, tmp_path / "cache", tag="RND")
    page = _render(mod, tmp_path / "cache", tag="RND", format="html")

    assert png["ok"] is False and "Pillow" in png["error"]
    assert page["ok"] is True and page["icons"]["pillow"] is False
    assert 'class="m"' in Path(page["outputs"]["html"]).read_text(encoding="utf-8")


def test_response_stays_inside_the_budget(mod: Path, tmp_path: Path) -> None:
    result = _render(mod, tmp_path / "cache", tag="RND", format="html", highlight=["X*"] * 500)

    assert len(json.dumps(result).encode()) <= BUDGET_BYTES
    assert len(result["warnings"]) <= 20


def test_segments_follow_the_preview_line_model() -> None:
    assert _segments((48, 65), (240, 325)) == [
        (48, 65, 48, 130),
        (48, 130, 240, 130),
        (240, 130, 240, 325),
    ]
    assert _segments((48, 65), (240, 65)) == [(48, 65, 240, 65)]
    assert _segments((48, 65), (48, 325)) == [(48, 65, 48, 325)]


def _six(text: str) -> float:
    return len(text) * 6.0


def test_labels_wrap_at_spaces_then_id_separators_then_anywhere() -> None:
    assert _wrap("Create The African Investment Bank", 90, _six) == [
        "Create The",
        "African",
        "Investment Bank",
    ]
    assert _wrap("AFRICAN_UNION_shared_focus_create", 90, _six) == [
        "AFRICAN_UNION_",
        "shared_focus_",
        "create",
    ]
    assert _wrap("Supercalifragilistic", 60, _six) == ["Supercalif", "ragilistic"]
    assert _wrap("", 60, _six) == []


def test_the_id_keeps_two_lines_and_cut_lines_end_in_dots() -> None:
    focus_id = "AFRICAN_UNION_shared_focus_create_stock_exchange_south_africa"
    name = "Create Stock Exchange In South Africa And Beyond"

    lines = _label_lines([focus_id, name], 180, _six)

    assert lines == [
        "AFRICAN_UNION_shared_focus_",
        "create_stock_exchange_south...",
        "Create Stock Exchange In So...",
    ]
    assert all(_six(line) <= 180 for line in lines)
    assert _label_lines(["SHORT_id", name], 180, _six) == ["SHORT_id", *_wrap(name, 180, _six)[:2]]
    assert _label_lines([focus_id], 90, _six)[-1].endswith("...")


def test_label_room_reaches_the_nearest_focus_in_the_row() -> None:
    room = _label_room({"a": (0, 0), "b": (2, 0), "c": (3, 0), "lone": (5, 1), "pair": (5, 2)})

    assert room["a"] == 2 * 96 - 8
    assert room["b"] == room["c"] == 96 - 8
    assert room["lone"] == room["pair"] == 3 * 96 - 8


def test_png_labels_have_accented_letters() -> None:
    from PIL import ImageFont

    font = _font(ImageFont, 12)

    if font.getbbox("c") == font.getbbox("\u0e01"):
        pytest.skip("no system font with more than ASCII glyphs")
    assert font.getmask("\u00e7").getbbox() != font.getmask("\u00e9").getbbox()


def test_long_labels_wrap_into_the_room_beside_them(
    mod: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import ImageDraw

    drawn: list[str] = []
    draw_text = ImageDraw.ImageDraw.text

    def record(self, xy, value, *args, **kwargs):
        drawn.append(value)
        return draw_text(self, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    long_id = "LNG_a_very_long_focus_identifier_that_needs_several_lines"
    (mod / "common" / "national_focus" / "lng.txt").write_text(
        f"""focus_tree = {{
    id = lng_tree
    focus = {{ id = {long_id} x = 0 y = 0 }}
    focus = {{ id = LNG_b x = 1 y = 0 }}
}}
""",
        encoding="utf-8",
    )

    result = _render(mod, tmp_path / "cache", tag="LNG", format="both", labels="id")

    page = Path(result["outputs"]["html"]).read_text(encoding="utf-8")
    label = (
        page.split(f'title="{long_id} (0, 0)')[1].split('<span class="t">')[1].split("</span>")[0]
    )
    lines = label.split("<br>")
    assert len(lines) == 3 and lines[-1].endswith("...")
    assert long_id not in label
    assert all(line in drawn for line in lines) and long_id not in drawn


@pytest.mark.parametrize(
    ("kwargs", "warned"),
    [
        ({"scale": 0.5}, True),
        ({"scale": 0.5, "labels": "none"}, False),
        ({"scale": 1}, False),
        ({"scale": 0.5, "format": "html"}, False),
    ],
)
def test_a_png_too_small_to_read_says_so(
    mod: Path, tmp_path: Path, kwargs: dict, warned: bool
) -> None:
    result = _render(mod, tmp_path / "cache", tag="RND", **kwargs)

    assert result["ok"] is True
    assert any("too small to read" in w for w in result["warnings"]) is warned


def _offset_tree(mod: Path, offsets: str, country: str = "OFS") -> None:
    country_block = f"country = {{ factor = 0 modifier = {{ add = 10 tag = {country} }} }}"
    (mod / "common" / "national_focus" / "ofs.txt").write_text(
        f"""focus_tree = {{
    id = ofs_tree
    {country_block if country else ""}
    focus = {{ id = OFS_a x = 0 y = 0 {offsets} }}
}}
""",
        encoding="utf-8",
    )


def _ofs_x(mod: Path, tmp_path: Path, **kwargs) -> tuple[int, dict]:
    kwargs.setdefault("tag", "OFS")
    result = _render(
        mod, tmp_path / "cache", format="html", focus_ids=["OFS_a"], margin=0, **kwargs
    )
    return result["region"]["min_x"], result


@pytest.mark.parametrize(
    ("trigger", "kwargs", "expected_x", "unevaluated"),
    [
        ("has_completed_focus = OFS_done", {}, 0, 0),
        ("has_completed_focus = OFS_done", {"completed": ["OFS_done"]}, 5, 0),
        ("date > 2010.1.1", {}, 0, 1),
        ("date > 2010.1.1", {"date": "2012.6.1"}, 5, 0),
        ("date > 2010.1.1", {"date": "2001.1.1"}, 0, 0),
        ("OR = { tag = AAA tag = OFS }", {}, 5, 0),
        ("NOT = { tag = OFS }", {}, 0, 0),
        ("has_focus_tree = ofs_tree", {}, 5, 0),
        ("has_focus_tree = other_tree", {}, 0, 0),
        ("NOT = { has_focus_tree = ofs_tree }", {}, 0, 0),
        ("OR = { has_focus_tree = other_tree has_focus_tree = ofs_tree }", {}, 5, 0),
        ("# always = no { }\n has_completed_focus = OFS_done", {"completed": ["OFS_done"]}, 5, 0),
    ],
)
def test_offsets_follow_the_scenario(
    scenario_mod: Path,
    tmp_path: Path,
    trigger: str,
    kwargs: dict,
    expected_x: int,
    unevaluated: int,
) -> None:
    _offset_tree(scenario_mod, f"offset = {{ x = 5 y = 0 trigger = {{ {trigger} }} }}")

    x, result = _ofs_x(scenario_mod, tmp_path, **kwargs)

    assert x == expected_x
    assert result["offsets"]["evaluator"] == EVALUATOR
    assert result["offsets"]["unevaluated"] == unevaluated


def test_the_scenario_is_reported(mod: Path, tmp_path: Path) -> None:
    _offset_tree(mod, "")

    _x, result = _ofs_x(mod, tmp_path, completed=["OFS_a", "OFS_b"], date="2012.6.1")

    assert result["scenario"] == {"completed": 2, "date": "2012.6.1"}
    assert result["country"] == "OFS" and result["country_source"] == "tree"


def test_an_offset_without_a_trigger_applies_as_upstream_decides(mod: Path, tmp_path: Path) -> None:
    _offset_tree(mod, "offset = { x = 3 y = 0 }")

    x, result = _ofs_x(mod, tmp_path)

    assert x == 3 and result["offsets"]["applied"] == 1


def test_an_undecidable_trigger_is_reported_not_applied(scenario_mod: Path, tmp_path: Path) -> None:
    _offset_tree(scenario_mod, "offset = { x = 4 y = 0 trigger = { has_country_flag = ofs_flag } }")

    x, result = _ofs_x(scenario_mod, tmp_path)

    assert x == 0
    assert result["offsets"]["unevaluated_sample"] == [{"focus": "OFS_a", "x": 4.0, "y": 0.0}]


@pytest.mark.parametrize(
    ("trigger", "expected_x", "unevaluated"),
    [
        ("tag = AAA", 0, 1),
        ("NOT = { tag = AAA }", 0, 1),
        ("NOT = { original_tag = AAA }", 0, 1),
        ("OR = { tag = AAA tag = BBB}", 0, 1),
        ("OR = { tag = AAA always = yes }", 5, 0),
        ("OR = { tag = AAA tag = BBB} has_completed_focus = OFS_never", 0, 0),
    ],
)
def test_with_no_country_tag_tests_stay_undecided(
    scenario_mod: Path, tmp_path: Path, trigger: str, expected_x: int, unevaluated: int
) -> None:
    _offset_tree(scenario_mod, f"offset = {{ x = 5 y = 0 trigger = {{ {trigger} }} }}", country="")

    x, result = _ofs_x(scenario_mod, tmp_path, tag=None, file="common/national_focus/ofs.txt")

    assert result["country"] is None
    assert x == expected_x
    assert result["offsets"]["unevaluated"] == unevaluated


def test_a_country_argument_decides_tag_tests(scenario_mod: Path, tmp_path: Path) -> None:
    _offset_tree(scenario_mod, "offset = { x = 5 y = 0 trigger = { NOT = { tag = AAA } } }", "")

    x, result = _ofs_x(
        scenario_mod, tmp_path, tag=None, file="common/national_focus/ofs.txt", country="BBB"
    )

    assert x == 5 and result["offsets"]["unevaluated"] == 0


def test_a_shared_focus_offset_follows_the_importing_tree(
    scenario_mod: Path, tmp_path: Path
) -> None:
    focus_dir = scenario_mod / "common" / "national_focus"
    (focus_dir / "hft_shared.txt").write_text(
        """shared_focus = {
    id = HFT_shared
    x = 0
    y = 0
    offset = { x = 6 y = 0 trigger = { has_focus_tree = hft_a } }
}
""",
        encoding="utf-8",
    )
    for name in ("hft_a", "hft_b"):
        (focus_dir / f"{name}.txt").write_text(
            f"focus_tree = {{\n    id = {name}\n    shared_focus = HFT_shared\n}}\n",
            encoding="utf-8",
        )
    cache = tmp_path / "cache"

    def shared_x(tree: str) -> int:
        result = _render(
            scenario_mod, cache, tree=tree, focus_ids=["HFT_shared"], margin=0, format="html"
        )
        assert result["ok"] is True, result
        return result["region"]["min_x"]

    assert shared_x("hft_a") == 6
    assert shared_x("hft_b") == 0


def test_fractional_coordinates_keep_offsets_and_icons(scenario_mod: Path, tmp_path: Path) -> None:
    (scenario_mod / "common" / "national_focus" / "frc.txt").write_text(
        """focus_tree = {
    id = frc_tree
    country = { factor = 0 modifier = { add = 10 tag = FRC } }
    focus = { id = FRC_half x = 1.5 y = 0 }
    focus = { id = FRC_moved x = 0 y = 1
        offset = { x = 2.5 y = 0 trigger = { has_completed_focus = FRC_done } }
        icon = { GFX_rnd_icon = { has_completed_focus = FRC_done } GFX_rnd_missing = yes } }
}
""",
        encoding="utf-8",
    )

    result = _render(
        scenario_mod,
        tmp_path / "cache",
        tag="FRC",
        format="html",
        focus_ids=["FRC_moved"],
        margin=0,
        completed=["FRC_done"],
    )

    assert result["offsets"] == {
        "evaluator": EVALUATOR,
        "applied": 1,
        "unevaluated": 0,
        "unevaluated_sample": [],
    }
    assert result["region"]["min_x"] == 2
    assert result["icons"]["resolved"] == 1 and result["icons"]["missing"] == 0
    assert result["warnings"] == []


def test_without_the_mod_evaluator_the_render_still_works(mod: Path, tmp_path: Path) -> None:
    shutil.rmtree(mod / "tools" / "analysis")
    _offset_tree(mod, "offset = { x = 5 y = 0 trigger = { tag = OFS } }")

    x, result = _ofs_x(mod, tmp_path)

    assert result["ok"] is True and x == 0
    assert result["offsets"]["evaluator"] is None
    assert result["warnings"][0].startswith("offsets and conditional icons were not evaluated")


@pytest.mark.parametrize(
    ("script_path", "patch", "reason"),
    [
        ("tools/analysis/focus_overlap_report.py", "evaluate = None", "missing evaluate"),
        ("tools/analysis/focus_overlap_report.py", "del Scenario", "missing Scenario"),
        ("tools/shared_utils.py", "del strip_comments", "missing shared_utils.strip_comments"),
        (
            "tools/shared_utils.py",
            "blank_quoted_strings = None",
            "missing shared_utils.blank_quoted_strings",
        ),
    ],
)
def test_a_changed_evaluator_api_is_reported(
    mod: Path, tmp_path: Path, script_path: str, patch: str, reason: str
) -> None:
    script = mod / script_path
    script.write_text(script.read_text(encoding="utf-8") + f"\n{patch}\n", encoding="utf-8")
    _offset_tree(mod, "offset = { x = 5 y = 0 trigger = { always = yes } }")

    x, result = _ofs_x(mod, tmp_path)

    assert result["ok"] is True and x == 0
    assert result["offsets"]["evaluator"] is None
    assert reason in result["warnings"][0]


@pytest.mark.parametrize(
    "body",
    [
        "raise RuntimeError('boom')",
        "return 'yes'",
        "return 1",
    ],
)
def test_an_evaluator_that_fails_leaves_the_offset_undecided(
    mod: Path, tmp_path: Path, body: str
) -> None:
    script = mod / "tools" / "analysis" / "focus_overlap_report.py"
    script.write_text(
        script.read_text(encoding="utf-8") + f"\n\ndef evaluate(body, scenario):\n    {body}\n",
        encoding="utf-8",
    )
    _offset_tree(mod, "offset = { x = 5 y = 0 trigger = { always = yes } }")

    x, result = _ofs_x(mod, tmp_path)

    assert x == 0
    assert result["offsets"]["applied"] == 0 and result["offsets"]["unevaluated"] == 1


@pytest.mark.parametrize("previous", [False, True])
def test_importing_the_evaluator_writes_no_bytecode_into_the_mod(
    mod: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous: bool
) -> None:
    monkeypatch.setattr(sys, "dont_write_bytecode", previous)
    _offset_tree(mod, "offset = { x = 5 y = 0 trigger = { always = yes } }")

    x, _result = _ofs_x(mod, tmp_path)

    assert x == 5
    assert not list((mod / "tools").rglob("__pycache__"))
    assert sys.dont_write_bytecode is previous


def test_a_scalar_offset_is_ignored(scenario_mod: Path, tmp_path: Path) -> None:
    _offset_tree(scenario_mod, "offset = yes offset = { x = 5 y = 0 trigger = { tag = OFS } }")

    x, result = _ofs_x(scenario_mod, tmp_path)

    assert x == 5 and result["offsets"]["applied"] == 1


def _icon_tree(mod: Path, icon: str) -> None:
    (mod / "common" / "national_focus" / "ico.txt").write_text(
        f"""focus_tree = {{
    id = ico_tree
    # A long comment before the gate: stripping it shifts every later offset.
    focus = {{ id = ICO_a x = 0 y = 0 # another comment with {{ braces }}
        {icon} }}
}}
""",
        encoding="utf-8",
    )


def _icons(mod: Path, tmp_path: Path, **kwargs) -> dict:
    return _render(mod, tmp_path / "cache", tag="ICO", format="html", **kwargs)["icons"]


def test_conditional_icons_follow_the_scenario(scenario_mod: Path, tmp_path: Path) -> None:
    _icon_tree(
        scenario_mod,
        "icon = { GFX_rnd_icon = { has_completed_focus = ICO_done } GFX_rnd_missing = yes }",
    )

    shown = _icons(scenario_mod, tmp_path, completed=["ICO_done"])
    fallback = _icons(scenario_mod, tmp_path)

    assert shown["resolved"] == 1 and shown["missing"] == 0
    assert fallback["resolved"] == 0
    assert fallback["missing_sample"] == ["GFX_rnd_missing"]


@pytest.mark.parametrize(
    ("icon", "kwargs", "shown"),
    [
        # A comment inside the gate is not part of it.
        (
            "icon = { GFX_rnd_icon = { # always = no\n has_completed_focus = ICO_done }\n"
            "GFX_rnd_missing = yes }",
            {"completed": ["ICO_done"]},
            "GFX_rnd_icon",
        ),
        # A gate the evaluator cannot decide is not taken.
        (
            "icon = { GFX_rnd_missing = { has_country_flag = ico_flag } GFX_rnd_icon = yes }",
            {},
            "GFX_rnd_icon",
        ),
        # When no gate holds, the first icon is drawn.
        (
            "icon = { GFX_rnd_missing = { has_completed_focus = ICO_never } "
            "GFX_rnd_icon = { has_completed_focus = ICO_never } }",
            {},
            "GFX_rnd_missing",
        ),
        # The old trigger/value form.
        (
            "icon = { trigger = { has_focus_tree = other_tree } value = GFX_rnd_missing }\n"
            "icon = { trigger = { has_focus_tree = ico_tree } value = GFX_rnd_icon }",
            {},
            "GFX_rnd_icon",
        ),
    ],
)
def test_the_icon_shown_is_the_first_whose_gate_holds(
    scenario_mod: Path, tmp_path: Path, icon: str, kwargs: dict, shown: str
) -> None:
    _icon_tree(scenario_mod, icon)

    icons = _icons(scenario_mod, tmp_path, **kwargs)

    if shown == "GFX_rnd_icon":
        assert icons["resolved"] == 1 and icons["missing"] == 0
    else:
        assert icons["missing_sample"] == [shown]


def test_a_shared_focus_icon_is_decided_in_its_own_file(scenario_mod: Path, tmp_path: Path) -> None:
    (scenario_mod / "common" / "national_focus" / "shr_shared.txt").write_text(
        """# Comments here move the shared focus away from where it would sit in rnd.txt.
# Another line.
shared_focus = {
    id = SHR_root
    x = 10
    y = 0
    icon = { GFX_rnd_missing = { has_completed_focus = SHR_never }
        GFX_rnd_icon = { has_completed_focus = SHR_done } }
}
""",
        encoding="utf-8",
    )

    result = _render(
        scenario_mod,
        tmp_path / "cache",
        tag="RND",
        format="html",
        focus_ids=["SHR_root"],
        margin=0,
        completed=["SHR_done"],
    )

    assert result["ok"] is True
    assert result["icons"]["resolved"] == 1 and result["icons"]["missing"] == 0


def test_a_file_with_two_trees_needs_tree(mod: Path, tmp_path: Path) -> None:
    two = mod / "common" / "national_focus" / "two.txt"
    two.write_text(
        _OTHER.replace("other_tree", "first_tree").replace("OTH_root", "TWO_a")
        + _OTHER.replace("other_tree", "second_tree").replace("OTH_root", "TWO_b"),
        encoding="utf-8",
    )
    cache = tmp_path / "cache"

    both = _render(mod, cache, file="common/national_focus/two.txt", format="html")
    one = _render(
        mod, cache, file="common/national_focus/two.txt", tree="second_tree", format="html"
    )

    assert both["ok"] is False and "pass tree=" in both["error"]
    assert [t["tree"] for t in both["trees"]] == ["first_tree", "second_tree"]
    assert one["ok"] is True and one["tree"]["id"] == "second_tree"


def test_icon_falls_back_between_dds_and_tga(mod: Path, tmp_path: Path) -> None:
    gfx = mod / "interface" / "rnd.gfx"
    gfx.write_text(_GFX.replace("rnd_icon.tga", "rnd_icon.dds"), encoding="utf-8")

    result = _render(mod, tmp_path / "cache", tag="RND", format="html")

    assert result["icons"]["resolved"] == 1
    assert result["icons"]["failed"] == 0


def test_unreadable_texture_is_reported(mod: Path, tmp_path: Path) -> None:
    (mod / "gfx" / "interface" / "goals" / "rnd_icon.tga").write_bytes(b"not an image")

    result = _render(mod, tmp_path / "cache", tag="RND", format="html")

    assert result["ok"] is True
    assert result["icons"]["failed"] == 1
    assert result["icons"]["failed_sample"][0].startswith("GFX_rnd_icon:")


def test_default_scale_keeps_the_png_inside_the_caps(
    mod: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(focus_render, "MAX_SIDE", 500)

    result = _render(mod, tmp_path / "cache", tag="RND")

    assert result["image"]["scale"] < 1
    assert max(result["image"]["width"], result["image"]["height"]) <= 500


def _slot(result: dict, x: int, y: int) -> tuple[int, int]:
    region = result["region"]
    return int((x - region["min_x"]) * 96), int((y - region["min_y"]) * 130 + 28)


def test_png_pixels_follow_the_preview(mod: Path, tmp_path: Path) -> None:
    from PIL import Image

    result = _render(mod, tmp_path / "cache", tag="RND", highlight=["RND_left"])
    image = Image.open(result["outputs"]["png"]).convert("RGB")
    root_left, root_top = _slot(result, 4, 0)
    left_left, left_top = _slot(result, 2, 1)
    end_left, end_top = _slot(result, 4, 2)

    # The 40x30 icon sits centred, 18 px above the slot's middle.
    assert image.getpixel((root_left + 48, root_top + 47)) == (200, 50, 50)
    # RND_root -> RND_left turns half a row below the root.
    assert image.getpixel((root_left - 48, root_top + 130)) == (0x88, 0xAA, 0xFF)
    # The RND_left / RND_right exclusive link runs red along their row.
    assert image.getpixel((root_left + 10, left_top + 65)) == (255, 0, 0)
    # RND_end's OR group is dashed: its turn row holds both line and background.
    row = {image.getpixel((x, left_top + 130)) for x in range(left_left + 60, end_left)}
    assert (0x88, 0xAA, 0xFF) in row and (0x1E, 0x1E, 0x1E) in row
    assert image.getpixel((left_left + 2, left_top + 40)) == (0xFF, 0xCC, 0x33)
    assert end_top > left_top


def test_whole_tree_gets_a_spare_column_each_side(mod: Path, tmp_path: Path) -> None:
    result = _render(mod, tmp_path / "cache", tag="RND", format="both")

    assert result["region"] == {"min_x": 1, "min_y": 0, "max_x": 16, "max_y": 2}
    assert result["icons"]["no_icon"] == 2
    assert result["bytes"]["png"] == Path(result["outputs"]["png"]).stat().st_size
    assert result["bytes"]["html"] == Path(result["outputs"]["html"]).stat().st_size


def test_explicit_scale_is_capped_with_a_warning(
    mod: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(focus_render, "MAX_SIDE", 600)

    result = _render(mod, tmp_path / "cache", tag="RND", scale=2)

    assert result["image"]["scale"] < 2
    assert max(result["image"]["width"], result["image"]["height"]) <= 600
    assert any(w.startswith("scale 2 lowered to") for w in result["warnings"])


def test_a_view_too_big_for_a_png_is_refused(
    mod: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(focus_render, "MAX_SIDE", 50)

    png = _render(mod, tmp_path / "cache", tag="RND")
    page = _render(mod, tmp_path / "cache", tag="RND", format="html")

    assert png["ok"] is False and "too big for one PNG" in png["error"]
    assert page["ok"] is True


def test_region_size_and_name_length_are_bounded(mod: Path, tmp_path: Path) -> None:
    wide = _render(mod, tmp_path / "cache", tag="RND", region=[0, 0, 5000, 1])
    long_name = _render(mod, tmp_path / "cache", file="x" * 400)

    assert wide["ok"] is False and "region spans at most" in wide["error"]
    assert long_name["ok"] is False and "at most 300 characters" in long_name["error"]


def test_empty_region_warns(mod: Path, tmp_path: Path) -> None:
    result = _render(mod, tmp_path / "cache", tag="RND", format="html", region=[100, 100, 101, 101])

    assert result["ok"] is True
    assert result["focuses"]["rendered"] == 0
    assert "the region holds no visible focus" in result["warnings"]


def test_decoder_errors_of_any_kind_are_reported(
    mod: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    real_open = Image.open

    def open_texture(fp, *args, **kwargs):
        if isinstance(fp, Path):
            raise NotImplementedError("Unimplemented DXGI format 91")
        return real_open(fp, *args, **kwargs)

    monkeypatch.setattr(Image, "open", open_texture)

    result = _render(mod, tmp_path / "cache", tag="RND", format="both")

    assert result["ok"] is True
    assert result["icons"]["failed_sample"] == ["GFX_rnd_icon: NotImplementedError"]


def test_a_damaged_icon_cache_entry_is_rebuilt(mod: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _render(mod, cache, tag="RND", format="html")
    (entry,) = (cache / "renders" / "icons").glob("*.png")
    entry.write_bytes(entry.read_bytes()[:20])

    result = _render(mod, cache, tag="RND", format="html")

    assert result["icons"]["resolved"] == 1
    assert entry.read_bytes().endswith(b"IEND\xaeB`\x82")
    assert not list((cache / "renders").glob(".*.tmp"))


def test_an_unwritable_cache_returns_an_error(mod: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "renders").write_text("not a directory", encoding="utf-8")

    result = _render(mod, cache, tag="RND", format="html")

    assert result["ok"] is False
    assert result["error"].startswith("Cannot write renders under")


def test_ref_finds_shared_files_at_the_revision(mod: Path, tmp_path: Path) -> None:
    init_repo(mod)
    git(mod, "add", "-A")
    git(mod, "commit", "-qm", "tree")
    (mod / "common" / "national_focus" / "shr_shared.txt").unlink()
    cache = tmp_path / "cache"

    before = _render(mod, cache, tag="RND", format="html", ref="HEAD")
    after = _render(mod, cache, tag="RND", format="html")

    assert before["focuses"]["in_tree"] == 6
    assert after["focuses"]["in_tree"] == 4


def test_every_mutually_exclusive_block_draws_a_link(mod: Path, tmp_path: Path) -> None:
    (mod / "common" / "national_focus" / "mtx.txt").write_text(
        """focus_tree = {
    id = mtx_tree
    focus = { id = MTX_a x = 0 y = 0
        mutually_exclusive = { focus = MTX_b }
        mutually_exclusive = { focus = MTX_c } }
    focus = { id = MTX_b x = 2 y = 0 }
    focus = { id = MTX_c x = 4 y = 0 }
}
""",
        encoding="utf-8",
    )

    result = _render(mod, tmp_path / "cache", tag="MTX", format="html")

    page = Path(result["outputs"]["html"]).read_text(encoding="utf-8")
    assert page.count("solid #ff0000") == 2


@pytest.mark.parametrize("hide", [["RND_root", "SHR_root"], ["*"]])
def test_hiding_everything_says_so(mod: Path, tmp_path: Path, hide: list[str]) -> None:
    result = _render(mod, tmp_path / "cache", tag="RND", format="html", hide_branches=hide)

    assert result["ok"] is False
    assert result["error"] == "hide_branches hid every focus in the tree."


def test_focus_ids_that_only_match_hidden_focuses_say_so(mod: Path, tmp_path: Path) -> None:
    result = _render(
        mod,
        tmp_path / "cache",
        tag="RND",
        format="html",
        focus_ids=["SHR_*"],
        hide_branches=["SHR_root"],
    )

    assert result["ok"] is False
    assert result["matched_hidden"] == ["SHR_child", "SHR_root"]


def test_the_hint_names_only_files_that_exist(mod: Path, tmp_path: Path) -> None:
    png = _render(mod, tmp_path / "cache", tag="RND")
    page = _render(mod, tmp_path / "cache", tag="RND", format="html")

    assert "html" not in png["hint"]
    assert "png" not in page["hint"]


@pytest.mark.integration
def test_real_mod_offsets_come_from_the_mod_evaluator(real_mod_root: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    result = render_focus_tree(
        real_mod_root,
        cache,
        FocusIndex(real_mod_root, cache),
        GfxIndex(real_mod_root, cache),
        LocalisationIndex(real_mod_root, cache),
        tree="georgia_focus",
        focus_ids=["USoE001"],
        margin=0,
        format="html",
    )

    assert result["ok"] is True
    assert result["offsets"]["evaluator"] == EVALUATOR
    assert result["offsets"]["applied"] >= 1
