"""Tests for the read-only upstream focus-ID rename wrapper (issue #187).

Unit tests plant a stand-in for upstream's pure `extract_focus_ids` /
`_replace_ids` helpers and route the wrapper's shim call in-process
(`inproc_shim`), so the real shim logic is exercised and measured by coverage.
Subprocess-failure, missing-helper, and live-stdio tests keep the genuine
subprocess/server paths. Integration tests run the real upstream module: the
equivalence oracle calls its file-writing `rename_focus_ids` entry point only
on a disposable copy under `tmp_path`, never on the real checkout.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

import md_mcp.tools.upstream_analysis as upstream_analysis
from md_mcp.tools import upstream_analysis_shim
from md_mcp.tools.upstream_analysis import rename_focus_ids_tool
from md_mcp.tools.upstream_analysis_shim import _CountingMapping
from md_mcp.util.response import BUDGET_BYTES, MAX_TEXT_BYTES
from tests.support.mod import make_mod_root

STANDIN_SCRIPT = '''\
"""Test stand-in for the upstream rename_focus_ids pure helpers."""
import re


def extract_focus_ids(text, tag):
    mapping = {}
    for line in text.splitlines():
        match = re.match(r"\\s*id\\s*=\\s*([A-Za-z0-9_?]+)", line)
        if match:
            old = match.group(1)
            prefix = tag + "_"
            mapping[old] = old if old.startswith(prefix) else prefix + old
    return mapping


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _replace_ids(line, mapping, *, localization=False, focus_file=False):
    def sub(match):
        return mapping.get(match.group(0), match.group(0))

    return _TOKEN_RE.sub(sub, line)
'''

BIG_MAPPING_SCRIPT = '''\
"""Test stand-in whose mapping alone exceeds the response budget."""


def extract_focus_ids(text, tag):
    return {f"OLD_{n:04d}": f"{tag}_NEW_{n:04d}" for n in range(4000)}


def _replace_ids(line, mapping, *, localization=False, focus_file=False):
    return line
'''

CLIPPING_SCRIPT = '''\
"""Test stand-in that rewrites the focus file far past the text budget."""


def extract_focus_ids(text, tag):
    return {f"{tag}_OLD": f"{tag}_NEW"}


def _replace_ids(line, mapping, *, localization=False, focus_file=False):
    if focus_file:
        return line + "X" * 120_000
    return line
'''

EMPTY_MAPPING_SCRIPT = '''\
"""Test stand-in that finds no focus IDs."""


def extract_focus_ids(text, tag):
    return {}


def _replace_ids(line, mapping, *, localization=False, focus_file=False):
    return line
'''

DUPLICATE_SCRIPT = '''\
"""Test stand-in whose extractor rejects duplicate normalized ids."""


def extract_focus_ids(text, tag):
    raise ValueError("duplicate normalized focus id OLD_a")


def _replace_ids(line, mapping, *, localization=False, focus_file=False):
    return line
'''

FOCUS_TEXT = (
    "tree = {\n"
    "\tfocus = {\n"
    "\t\tid = OLD_a\n"
    "\t\trelative_position_id = OLD_b\n"
    "\t}\n"
    "\tfocus = {\n"
    "\t\tid = OLD_b\n"
    "\t\tname = OLD_a\n"
    "\t}\n"
    "\tfocus = {\n"
    "\t\tid = TST_prefixed\n"
    "\t}\n"
    "}\n"
    "Focus OLD_a note\n"
)
EVENTS_TEXT = (
    "country_event = {\n"
    "\tid = TESTNS.1\n"
    "}\n"
    "FOCUS = OLD_a\n"
    "complete_national_focus = OLD_b\n"
    "has_completed_focus = OLD_a\n"
)
GUI_TEXT = "gui = {\n\tshared_focus = OLD_b\n}\n"
GFX_TEXT = "gfx = {\n\tFOCUS = OLD_b\n}\n"
MOD_TEXT = "mod_ref = {\n\tFOCUS = OLD_a\n}\n"
LOC_TEXT = 'l_english:\n OLD_a: "One"\n OLD_a_desc: "One desc"\n TST_prefixed: "P"\n'
LOC2_TEXT = 'l_english:\n OLD_b: "Two"\n OLD_b_desc: "Two desc"\n'


def _plant_script(mod_root: Path, text: str) -> None:
    directory = mod_root / "tools" / "standardization"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rename_focus_ids.py").write_text(text, encoding="utf-8")


def _build_fixture(mod_root: Path, *, crlf: bool = False, bom: bool = False) -> None:
    """Refs across common/, events/, history/, interface/ plus a localisation file."""
    (mod_root / "common" / "national_focus").mkdir(parents=True, exist_ok=True)
    (mod_root / "common" / "national_focus" / "tree.txt").write_text(FOCUS_TEXT, encoding="utf-8")
    (mod_root / "common" / "identity_refs.txt").write_text(
        "FOCUS = TST_prefixed\n", encoding="utf-8"
    )
    (mod_root / "common" / "bom_refs.txt").write_bytes(b"\xef\xbb\xbf" + b"FOCUS = OLD_a\n")
    (mod_root / "events").mkdir(parents=True, exist_ok=True)
    (mod_root / "events" / "refs.txt").write_text(EVENTS_TEXT, encoding="utf-8")
    (mod_root / "history").mkdir(parents=True, exist_ok=True)
    ends = "\r\n" if crlf else "\n"
    (mod_root / "history" / "crlf.txt").write_text(
        "\tactivate_shine_on_focus = OLD_a" + ends, encoding="utf-8", newline=""
    )
    (mod_root / "interface").mkdir(parents=True, exist_ok=True)
    (mod_root / "interface" / "panel.gui").write_text(GUI_TEXT, encoding="utf-8")
    (mod_root / "interface" / "panel.gfx").write_text(GFX_TEXT, encoding="utf-8")
    (mod_root / "interface" / "summary.mod").write_text(MOD_TEXT, encoding="utf-8")
    (mod_root / "localisation" / "english").mkdir(parents=True, exist_ok=True)
    raw = LOC_TEXT.encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    (mod_root / "localisation" / "english" / "test_l_english.yml").write_bytes(raw)
    # A second localisation file without a BOM still changes, and upstream would
    # write it back with a UTF-8 BOM, so its preview must say so.
    (mod_root / "localisation" / "english" / "test2_l_english.yml").write_bytes(
        LOC2_TEXT.encode("utf-8")
    )


@pytest.fixture
def unit_mod(tmp_path: Path) -> Path:
    mod_root = make_mod_root(tmp_path / "mod")
    _build_fixture(mod_root, crlf=True, bom=True)
    _plant_script(mod_root, STANDIN_SCRIPT)
    return mod_root


@pytest.fixture
def inproc_shim(monkeypatch: pytest.MonkeyPatch):
    """Dispatch the wrapper's shim call in-process; restore bytecode state.

    The production wrapper spawns the shim as a subprocess, which pytest
    coverage cannot see into; the fixture routes `_run_shim` through the real
    `upstream_analysis_shim.run` instead. The shim flips
    `sys.dont_write_bytecode` while loading upstream helpers, and the snapshot
    is restored so later tests keep their own bytecode behaviour.
    """

    previous = sys.dont_write_bytecode

    def run_in_process(mod_root: Path, operation: str, payload: dict, *, timeout: int) -> dict:
        del timeout
        return upstream_analysis_shim.run(operation, mod_root, payload)

    monkeypatch.setattr(upstream_analysis, "_run_shim", run_in_process)
    yield
    sys.dont_write_bytecode = previous


FOCUS_REL = "common/national_focus/tree.txt"
LOC_REL = "localisation/english/test_l_english.yml"
LOC2_REL = "localisation/english/test2_l_english.yml"


def test_md_mcp_import_resolves_under_this_worktree() -> None:
    import md_mcp

    repo_root = Path(__file__).resolve().parents[1]
    assert Path(md_mcp.__file__).resolve().is_relative_to(repo_root)


def test_plan_matches_standin_and_pages_files(unit_mod: Path, inproc_shim: None) -> None:
    plan = rename_focus_ids_tool(unit_mod, FOCUS_REL, "tst", loc_file=LOC_REL)

    assert plan["ok"] is True
    assert plan["tag"] == "TST"
    assert plan["focus_file"] == FOCUS_REL
    assert plan["focus_changed"] is True
    assert plan["loc_file"] == LOC_REL
    assert plan["loc_changed"] is True
    assert plan["mapping_count"] == 3
    assert plan["mapping"] == {
        "OLD_a": "TST_OLD_a",
        "OLD_b": "TST_OLD_b",
        "TST_prefixed": "TST_prefixed",
    }
    assert plan["total"] == plan["changed_total"] == 8
    assert plan["truncated"] is False
    assert [entry["path"] for entry in plan["files"]] == [
        "common/bom_refs.txt",
        FOCUS_REL,
        "events/refs.txt",
        "history/crlf.txt",
        "interface/panel.gfx",
        "interface/panel.gui",
        "interface/summary.mod",
        LOC_REL,
    ]
    # The already-canonical id counts no replacement and its refs-only file stays out.
    assert all(entry["root"] == "mod" for entry in plan["files"])
    assert [entry["replacements"] for entry in plan["files"]] == [1, 5, 3, 1, 1, 1, 1, 1]
    assert "common/identity_refs.txt" not in [entry["path"] for entry in plan["files"]]

    page = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", loc_file=LOC_REL, limit=2, offset=2)
    assert page["total"] == 8
    assert page["returned"] == 2
    assert page["truncated"] is True
    assert [entry["path"] for entry in page["files"]] == [
        "events/refs.txt",
        "history/crlf.txt",
    ]
    tail = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", loc_file=LOC_REL, limit=2, offset=8)
    assert tail["returned"] == 0
    assert tail["truncated"] is False


def test_text_mode_returns_full_new_text(unit_mod: Path, inproc_shim: None) -> None:
    focus = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", path=FOCUS_REL)
    assert focus["ok"] is True
    assert focus["kind"] == "focus"
    assert focus["root"] == "mod"
    assert focus["changed"] is True
    assert focus["mapping_count"] == 3
    assert focus["txt_truncated"] is False
    assert "txt_bytes" in focus
    assert "\t\tid = TST_OLD_a\n" in focus["txt"]
    assert "\t\trelative_position_id = TST_OLD_b\n" in focus["txt"]
    assert "\t\tid = TST_OLD_b\n" in focus["txt"]
    assert "\t\tname = TST_OLD_a\n" in focus["txt"]
    assert "Focus TST_OLD_a note\n" in focus["txt"]
    assert "id = TST_prefixed\n" in focus["txt"]

    history = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", path="history/crlf.txt")
    assert history["txt"] == "\tactivate_shine_on_focus = TST_OLD_a\r\n"
    assert history["changed"] is True

    bom_refs = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", path="common/bom_refs.txt")
    assert bom_refs["had_bom"] is True
    assert "retains" in bom_refs["note"]
    # Script files keep their BOM bytes: the BOM character stays inside txt.
    assert bom_refs["txt"].startswith("\ufeff")
    assert bom_refs["txt"].endswith("FOCUS = TST_OLD_a\n")

    loc = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", loc_file=LOC_REL, path=LOC_REL)
    assert loc["kind"] == "loc"
    assert loc["had_bom"] is True
    assert loc["changed"] is True
    assert "write the file back with one" in loc["note"]
    assert not loc["txt"].startswith("\ufeff")
    assert ' TST_OLD_a: "One"' in loc["txt"]
    # The stand-in does not model upstream's `_desc` lookahead; the real helper
    # rewrites `OLD_a_desc` in the integration fixture below.
    assert ' OLD_a_desc: "One desc"' in loc["txt"]

    no_bom_loc = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", loc_file=LOC2_REL, path=LOC2_REL)
    assert no_bom_loc["kind"] == "loc"
    assert no_bom_loc["changed"] is True
    assert "had_bom" not in no_bom_loc
    assert no_bom_loc["note"] == (
        "upstream writes changed localisation back with a UTF-8 BOM; write the file back with one"
    )
    assert not no_bom_loc["txt"].startswith("\ufeff")
    assert ' TST_OLD_b: "Two"' in no_bom_loc["txt"]

    unchanged = rename_focus_ids_tool(
        unit_mod, FOCUS_REL, "TST", loc_file=LOC_REL, path="common/identity_refs.txt"
    )
    assert unchanged["ok"] is True
    assert unchanged["changed"] is False
    assert unchanged["txt"] == "FOCUS = TST_prefixed\n"

    outside = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", path="descriptor.mod")
    assert outside["ok"] is False
    assert "path is not part of the rename plan" in outside["error"]


def test_user_paths_resolve_submod_overlay_first(tmp_path: Path, inproc_shim: None) -> None:
    base = make_mod_root(tmp_path / "base")
    submod = tmp_path / "submod"
    submod.mkdir()
    _plant_script(base, STANDIN_SCRIPT)
    (base / FOCUS_REL).parent.mkdir(parents=True, exist_ok=True)
    (base / FOCUS_REL).write_text(
        "tree = {\n\tfocus = {\n\t\tid = BASE_only_focus\n\t}\n}\n", encoding="utf-8"
    )
    (submod / FOCUS_REL).parent.mkdir(parents=True, exist_ok=True)
    (submod / FOCUS_REL).write_text(FOCUS_TEXT, encoding="utf-8")
    (base / "events").mkdir(parents=True, exist_ok=True)
    (submod / "events").mkdir(parents=True, exist_ok=True)
    (base / "events" / "base_only.txt").write_text("FOCUS = OLD_a\n", encoding="utf-8")
    # Same relative path in both roots: the overlay shadows the base copy.
    (base / "events" / "shadowed.txt").write_text("FOCUS = BASE_only_focus\n", encoding="utf-8")
    (submod / "events" / "shadowed.txt").write_text("FOCUS = OLD_b\n", encoding="utf-8")

    plan = rename_focus_ids_tool(base, FOCUS_REL, "TST", submod_root=submod)

    assert plan["ok"] is True
    assert plan["mapping"] == {
        "OLD_a": "TST_OLD_a",
        "OLD_b": "TST_OLD_b",
        "TST_prefixed": "TST_prefixed",
    }
    assert "BASE_only_focus" not in plan["mapping"]
    owners = {entry["path"]: entry["root"] for entry in plan["files"]}
    assert owners[FOCUS_REL] == "submod"
    assert owners["events/base_only.txt"] == "mod"
    assert owners["events/shadowed.txt"] == "submod"
    assert all(
        entry["root"] == "submod"
        for entry in plan["files"]
        if entry["path"] == "events/shadowed.txt"
    )


def test_absolute_base_focus_shadowed_by_overlay_is_refused(
    tmp_path: Path, inproc_shim: None
) -> None:
    base = make_mod_root(tmp_path / "base")
    submod = tmp_path / "submod"
    _plant_script(base, STANDIN_SCRIPT)
    (base / FOCUS_REL).parent.mkdir(parents=True, exist_ok=True)
    (base / FOCUS_REL).write_text(
        "tree = {\n\tfocus = {\n\t\tid = OLD_a\n\t}\n}\n", encoding="utf-8"
    )
    (submod / FOCUS_REL).parent.mkdir(parents=True, exist_ok=True)
    (submod / FOCUS_REL).write_text(
        "tree = {\n\tfocus = {\n\t\tid = OLD_a\n\t}\n}\n", encoding="utf-8"
    )
    (submod / "events").mkdir(parents=True, exist_ok=True)
    (submod / "events" / "refs.txt").write_text(
        "complete_national_focus = OLD_a\n", encoding="utf-8"
    )

    plan = rename_focus_ids_tool(base, str(base / FOCUS_REL), "TST", submod_root=submod)

    assert plan["ok"] is False
    assert "focus_file" in plan["error"]
    assert "shadowed" in plan["error"]

    preview = rename_focus_ids_tool(
        base, str(base / FOCUS_REL), "TST", submod_root=submod, path=str(base / FOCUS_REL)
    )

    assert preview["ok"] is False
    assert "focus_file" in preview["error"]
    assert "shadowed" in preview["error"]


@pytest.mark.parametrize(
    "shadow_kind",
    ["file", "directory", "dangling link", "link outside"],
)
def test_absolute_base_localisation_shadowed_by_overlay_is_refused(
    tmp_path: Path, shadow_kind: str, inproc_shim: None
) -> None:
    base = make_mod_root(tmp_path / "base")
    submod = tmp_path / "submod"
    _plant_script(base, STANDIN_SCRIPT)
    (base / FOCUS_REL).parent.mkdir(parents=True, exist_ok=True)
    (base / FOCUS_REL).write_text(
        "tree = {\n\tfocus = {\n\t\tid = OLD_a\n\t}\n}\n", encoding="utf-8"
    )
    (base / LOC_REL).parent.mkdir(parents=True, exist_ok=True)
    (base / LOC_REL).write_text('l_english:\n OLD_a: "One"\n', encoding="utf-8")
    (submod / LOC_REL).parent.mkdir(parents=True, exist_ok=True)
    shadow = submod / LOC_REL
    if shadow_kind == "file":
        # An active real overlay localisation file shadows the selected base
        # copy even though `.yml` never joins the scanned set.
        shadow.write_text('l_english:\n OLD_a: "Overlay One"\n', encoding="utf-8")
    elif shadow_kind == "directory":
        shadow.mkdir()
    elif shadow_kind == "dangling link":
        shadow.symlink_to(tmp_path / "absent_l_english.yml")
    else:
        outside = tmp_path / "outside_l_english.yml"
        outside.write_text('l_english:\n OLD_a: "Outside"\n', encoding="utf-8")
        shadow.symlink_to(outside)

    plan = rename_focus_ids_tool(
        base, FOCUS_REL, "TST", submod_root=submod, loc_file=str(base / LOC_REL)
    )

    assert plan["ok"] is False
    assert "loc_file" in plan["error"]
    assert "shadowed" in plan["error"]


def test_unshadowed_absolute_base_source_is_allowed(tmp_path: Path, inproc_shim: None) -> None:
    base = make_mod_root(tmp_path / "base")
    submod = tmp_path / "submod"
    submod.mkdir()
    _plant_script(base, STANDIN_SCRIPT)
    (base / FOCUS_REL).parent.mkdir(parents=True, exist_ok=True)
    (base / FOCUS_REL).write_text(
        "tree = {\n\tfocus = {\n\t\tid = OLD_a\n\t}\n}\n", encoding="utf-8"
    )
    (base / "events").mkdir(parents=True, exist_ok=True)
    (base / "events" / "refs.txt").write_text("complete_national_focus = OLD_a\n", encoding="utf-8")

    plan = rename_focus_ids_tool(base, str(base / FOCUS_REL), "TST", submod_root=submod)

    assert plan["ok"] is True
    assert plan["focus_file"] == FOCUS_REL
    assert plan["focus_changed"] is True
    assert {entry["path"] for entry in plan["files"]} == {
        FOCUS_REL,
        "events/refs.txt",
    }


def test_scan_rejects_links_outside_the_scan_roots(
    unit_mod: Path, tmp_path: Path, inproc_shim: None
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("FOCUS = OLD_b\n", encoding="utf-8")
    (unit_mod / "events" / "link_out.txt").symlink_to(outside)

    plan = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST")

    assert plan["ok"] is False
    assert "events/link_out.txt" in plan["error"]
    assert "links outside the mod scan roots" in plan["error"]


def test_inside_root_scan_symlinks_stay_previewable(unit_mod: Path, inproc_shim: None) -> None:
    (unit_mod / "events" / "link_in.txt").symlink_to(unit_mod / "events" / "refs.txt")

    plan = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST")

    assert plan["ok"] is True
    # The link resolves into the plan as the same relative path, not a duplicate.
    assert [entry["path"] for entry in plan["files"]].count("events/refs.txt") == 1
    assert "events/link_in.txt" not in [entry["path"] for entry in plan["files"]]

    preview = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", path="events/link_in.txt")
    assert preview["ok"] is True
    assert preview["path"] == "events/refs.txt"
    assert "complete_national_focus = TST_OLD_b" in preview["txt"]


def test_scan_rejects_a_symlinked_scan_base(
    unit_mod: Path, tmp_path: Path, inproc_shim: None
) -> None:
    outside = tmp_path / "outside_tree"
    outside.mkdir()
    (outside / "refs.txt").write_text("FOCUS = OLD_b\n", encoding="utf-8")
    (unit_mod / "history").rename(tmp_path / "real_history")
    (unit_mod / "history").symlink_to(outside, target_is_directory=True)

    plan = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST")

    assert plan["ok"] is False
    assert "history/ links outside the mod scan roots" in plan["error"]


def test_scan_rejects_symlink_targets_outside_the_scan_trees(
    unit_mod: Path, inproc_shim: None
) -> None:
    (unit_mod / "common" / "tree_link.txt").symlink_to(unit_mod / "descriptor.mod")

    plan = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST")

    assert plan["ok"] is False
    assert "common/tree_link.txt resolves to descriptor.mod" in plan["error"]


def test_rejects_hostile_or_invalid_paths(unit_mod: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("id = OLD_a\n", encoding="utf-8")

    traversal = rename_focus_ids_tool(unit_mod, "../outside.txt", "TST")
    assert traversal["ok"] is False
    assert "outside the allowed content roots" in traversal["error"]

    absolute = rename_focus_ids_tool(unit_mod, str(outside), "TST")
    assert absolute["ok"] is False
    assert "outside the allowed content roots" in absolute["error"]

    escape_link = unit_mod / "common" / "escape.txt"
    escape_link.symlink_to(outside)
    symlink = rename_focus_ids_tool(unit_mod, "common/escape.txt", "TST")
    assert symlink["ok"] is False
    assert "outside the allowed content roots" in symlink["error"]

    missing = rename_focus_ids_tool(unit_mod, "common/national_focus/absent.txt", "TST")
    assert missing["ok"] is False
    assert "not a regular file" in missing["error"]

    bad_loc = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", loc_file="events/refs.txt")
    assert bad_loc["ok"] is False
    assert "unsupported extension" in bad_loc["error"]

    identity = unit_mod / "common" / "identity_refs.bin"
    identity.write_text("x = 1\n", encoding="utf-8")
    bad_path = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", path="common/identity_refs.bin")
    assert bad_path["ok"] is False
    assert "unsupported extension" in bad_path["error"]

    bad_tag = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TOOLONG")
    assert bad_tag["ok"] is False
    assert "tag must be a 2-4 character country tag" in bad_tag["error"]

    bad_limit = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST", limit=cast(Any, "abc"))
    assert bad_limit["ok"] is False
    assert "limit must be an integer" in bad_limit["error"]

    empty = rename_focus_ids_tool(unit_mod, "   ", "TST")
    assert empty["ok"] is False
    assert "focus_file must name one mod .txt focus file" in empty["error"]


def test_missing_upstream_helper_is_a_shaped_error(tmp_path: Path) -> None:
    mod_root = make_mod_root(tmp_path / "mod")
    focus = mod_root / FOCUS_REL
    focus.parent.mkdir(parents=True)
    focus.write_text(FOCUS_TEXT, encoding="utf-8")

    result = rename_focus_ids_tool(mod_root, FOCUS_REL, "TST")

    assert result["ok"] is False
    assert mod_root.name in result["error"]
    assert "rename_focus_ids.py" in result["error"]


def test_unloadable_or_incomplete_upstream_helper_is_a_shaped_error(tmp_path: Path) -> None:
    mod_root = make_mod_root(tmp_path / "mod")
    _build_fixture(mod_root)

    _plant_script(mod_root, "raise ImportError('boom')\n")
    broken = rename_focus_ids_tool(mod_root, FOCUS_REL, "TST")
    assert broken["ok"] is False
    assert "Could not load upstream focus ID rename APIs" in broken["error"]

    _plant_script(mod_root, "X = 1\n")
    incomplete = rename_focus_ids_tool(mod_root, FOCUS_REL, "TST")
    assert incomplete["ok"] is False
    assert "Upstream focus ID rename API is missing" in incomplete["error"]


def test_no_focus_ids_is_a_bounded_error(unit_mod: Path, inproc_shim: None) -> None:
    _plant_script(unit_mod, EMPTY_MAPPING_SCRIPT)

    result = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST")

    assert result["ok"] is False
    assert result["error"] == "No focus IDs found in tree.txt"


def test_oversized_mapping_is_dropped_before_files(tmp_path: Path, inproc_shim: None) -> None:
    mod_root = make_mod_root(tmp_path / "mod")
    _plant_script(mod_root, BIG_MAPPING_SCRIPT)
    _build_fixture(mod_root)

    plan = rename_focus_ids_tool(mod_root, FOCUS_REL, "TST")

    assert plan["ok"] is True
    assert plan["mapping_count"] == 4000
    assert "mapping" not in plan
    assert plan["mapping_dropped"] == 4000
    assert plan["size_truncated"] is True
    assert plan["total"] == 0
    assert plan["files"] == []
    assert len(json.dumps(plan, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_oversized_text_is_clipped_and_warned(tmp_path: Path, inproc_shim: None) -> None:
    mod_root = make_mod_root(tmp_path / "mod")
    _plant_script(mod_root, CLIPPING_SCRIPT)
    _build_fixture(mod_root)

    result = rename_focus_ids_tool(mod_root, FOCUS_REL, "TST", path=FOCUS_REL)

    assert result["ok"] is True
    assert result["txt_truncated"] is True
    assert result["txt_bytes"] > MAX_TEXT_BYTES
    assert "do NOT write clipped content back" in result["note"]
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_subprocess_failure_shapes(unit_mod: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(upstream_analysis, "run_in_group", timed_out)
    timeout = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST")
    assert timeout["ok"] is False
    assert timeout["error"].startswith("rename_focus_ids timed out after 300s:")

    def spawn_failure(*args: Any, **kwargs: Any) -> Any:
        raise OSError("exec failed")

    monkeypatch.setattr(upstream_analysis, "run_in_group", spawn_failure)
    spawn = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST")
    assert spawn["ok"] is False
    assert "Could not run rename_focus_ids" in spawn["error"]

    def nonzero_exit(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=3, stdout="", stderr="boom upstream\n")

    monkeypatch.setattr(upstream_analysis, "run_in_group", nonzero_exit)
    failed = rename_focus_ids_tool(unit_mod, FOCUS_REL, "TST")
    assert failed["ok"] is False
    assert "exited with code 3: boom upstream" in failed["error"]


# ---------------------------------------------------------------------------
# Direct in-process shim coverage
#
# These tests call `upstream_analysis_shim.run` directly for the wrapper's
# reachable error branches; the `inproc_shim` fixture keeps the global
# bytecode flag from leaking between tests.
# ---------------------------------------------------------------------------


def _shim_payload(mod_root: Path, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "focus_file": str(mod_root / FOCUS_REL),
        "tag": "TST",
        "allowed_roots": [str(mod_root)],
        "root_labels": ["mod"],
        "limit": 50,
        "offset": 0,
    }
    payload.update(overrides)
    return payload


def test_oversized_files_page_reconciles_returned_and_truncated(
    tmp_path: Path, inproc_shim: None
) -> None:
    mod_root = make_mod_root(tmp_path / "mod")
    _plant_script(mod_root, STANDIN_SCRIPT)
    focus = mod_root / FOCUS_REL
    focus.parent.mkdir(parents=True)
    focus.write_text("focus = {\n\tid = OLD_b\n}\n", encoding="utf-8")
    (mod_root / "events").mkdir()
    for index in range(2400):
        (mod_root / "events" / f"bulk_{index:04d}.txt").write_text(
            "complete_national_focus = OLD_b\n", encoding="utf-8"
        )

    small = rename_focus_ids_tool(mod_root, FOCUS_REL, "TST", limit=3)

    assert small["ok"] is True
    assert small["total"] == small["changed_total"] == 2401
    assert small["returned"] == 3
    assert small["truncated"] is True
    assert small["files"][0]["path"] == FOCUS_REL
    assert "size_truncated" not in small

    oversized = rename_focus_ids_tool(mod_root, FOCUS_REL, "TST", limit=3000)

    assert oversized["ok"] is True
    assert oversized["total"] == 2401
    assert oversized["mapping_count"] == 1
    assert "mapping" not in oversized
    assert oversized["mapping_dropped"] == 1
    assert "files" not in oversized
    assert oversized["files_dropped"] == 2401
    assert oversized["size_truncated"] is True
    # The wrapper keeps the pagination fields honest when the drop removes the
    # whole files page instead of leaving a stale returned/truncated pair.
    assert oversized["returned"] == 0
    assert oversized["truncated"] is True
    assert len(json.dumps(oversized, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_shim_refuses_invalid_scan_roots(tmp_path: Path, inproc_shim: None) -> None:
    mod_root = make_mod_root(tmp_path / "mod")
    _plant_script(mod_root, STANDIN_SCRIPT)

    bare = upstream_analysis_shim.run("rename_focus_ids", mod_root, {"tag": "TST"})

    assert bare["ok"] is False
    assert bare["error"] == "no mod scan roots were provided"

    non_list = upstream_analysis_shim.run(
        "rename_focus_ids",
        mod_root,
        _shim_payload(mod_root, allowed_roots=cast(Any, "mod")),
    )

    assert non_list["error"] == "no mod scan roots were provided"

    absent = upstream_analysis_shim.run(
        "rename_focus_ids",
        mod_root,
        _shim_payload(mod_root, allowed_roots=[str(tmp_path / "none")]),
    )

    assert absent["ok"] is False
    assert "scan root does not exist" in absent["error"]

    not_dir = tmp_path / "plain.txt"
    not_dir.write_text("x = 1\n", encoding="utf-8")
    refused = upstream_analysis_shim.run(
        "rename_focus_ids",
        mod_root,
        _shim_payload(mod_root, allowed_roots=[str(not_dir)]),
    )

    assert refused["ok"] is False
    assert "scan root is not a directory" in refused["error"]


def test_shim_refuses_hostile_paths_tags_and_pagination(
    unit_mod: Path, tmp_path: Path, inproc_shim: None
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("id = OLD_a\n", encoding="utf-8")

    not_text = upstream_analysis_shim.run(
        "rename_focus_ids",
        unit_mod,
        _shim_payload(unit_mod, focus_file=123),
    )

    assert not_text["ok"] is False
    assert not_text["error"] == "focus_file must be a mod file path"

    missing = upstream_analysis_shim.run(
        "rename_focus_ids",
        unit_mod,
        _shim_payload(unit_mod, focus_file=str(unit_mod / "events/absent.txt")),
    )

    assert missing["ok"] is False
    assert "focus_file does not resolve to an existing file" in missing["error"]

    outside_file = upstream_analysis_shim.run(
        "rename_focus_ids",
        unit_mod,
        _shim_payload(unit_mod, focus_file=str(outside)),
    )

    assert outside_file["error"] == "focus_file is outside the mod scan roots"

    loc_outside = upstream_analysis_shim.run(
        "rename_focus_ids",
        unit_mod,
        _shim_payload(unit_mod, loc_file=str(outside)),
    )

    assert loc_outside["error"] == "loc_file is outside the mod scan roots"

    loc_wrong_suffix = upstream_analysis_shim.run(
        "rename_focus_ids",
        unit_mod,
        _shim_payload(unit_mod, loc_file=str(unit_mod / "events/refs.txt")),
    )

    assert "must be an existing regular file with one of" in loc_wrong_suffix["error"]

    path_outside = upstream_analysis_shim.run(
        "rename_focus_ids",
        unit_mod,
        _shim_payload(unit_mod, path=str(outside)),
    )

    assert path_outside["error"] == "path is outside the mod scan roots"

    path_missing = upstream_analysis_shim.run(
        "rename_focus_ids",
        unit_mod,
        _shim_payload(unit_mod, path=str(unit_mod / "events/absent.txt")),
    )

    assert "path does not resolve to an existing file" in path_missing["error"]

    bad_tag = upstream_analysis_shim.run(
        "rename_focus_ids", unit_mod, _shim_payload(unit_mod, tag="T!")
    )

    assert bad_tag["error"] == "tag must be a 2-4 character country tag"

    bad_limit = upstream_analysis_shim.run(
        "rename_focus_ids", unit_mod, _shim_payload(unit_mod, limit=cast(Any, "abc"))
    )

    assert "Invalid pagination values" in bad_limit["error"]


def test_shim_rejects_invalid_utf8_focus_and_localisation(
    unit_mod: Path, inproc_shim: None
) -> None:
    (unit_mod / FOCUS_REL).write_bytes(b"\xff\xfe\x00")

    broken_focus = upstream_analysis_shim.run("rename_focus_ids", unit_mod, _shim_payload(unit_mod))

    assert broken_focus["ok"] is False
    assert "focus file is not valid UTF-8" in broken_focus["error"]

    (unit_mod / FOCUS_REL).write_text("focus = {\n\tid = OLD_a\n}\n", encoding="utf-8")
    (unit_mod / LOC_REL).write_bytes(b"\xff\xfe")

    broken_loc = upstream_analysis_shim.run(
        "rename_focus_ids",
        unit_mod,
        _shim_payload(unit_mod, loc_file=str(unit_mod / LOC_REL)),
    )

    assert broken_loc["ok"] is False
    assert "test_l_english.yml is not valid utf-8-sig text" in broken_loc["error"]


def test_shim_passes_upstream_duplicate_id_error_through(tmp_path: Path, inproc_shim: None) -> None:
    mod_root = make_mod_root(tmp_path / "mod")
    _build_fixture(mod_root)
    _plant_script(mod_root, DUPLICATE_SCRIPT)

    result = upstream_analysis_shim.run("rename_focus_ids", mod_root, _shim_payload(mod_root))

    assert result["ok"] is False
    assert result["error"] == "duplicate normalized focus id OLD_a"


def test_shim_reports_missing_or_incomplete_upstream_helper(
    tmp_path: Path, inproc_shim: None
) -> None:
    mod_root = make_mod_root(tmp_path / "mod")

    absent = upstream_analysis_shim.run("rename_focus_ids", mod_root, _shim_payload(mod_root))

    assert absent["ok"] is False
    assert absent["unsupported"] is True
    assert "rename_focus_ids.py" in absent["error"]

    _plant_script(mod_root, "raise ImportError('boom')\n")
    broken = upstream_analysis_shim.run("rename_focus_ids", mod_root, _shim_payload(mod_root))

    assert broken["ok"] is False
    assert broken["unsupported"] is True
    assert "Could not load upstream focus ID rename APIs" in broken["error"]

    _plant_script(mod_root, "X = 1\n")
    incomplete = upstream_analysis_shim.run("rename_focus_ids", mod_root, _shim_payload(mod_root))

    assert incomplete["ok"] is False
    assert incomplete["error"] == (
        "Upstream focus ID rename API is missing: extract_focus_ids, _replace_ids"
    )


def test_shim_plans_without_root_labels_or_pagination(tmp_path: Path, inproc_shim: None) -> None:
    mod_root = make_mod_root(tmp_path / "mod")
    _plant_script(mod_root, STANDIN_SCRIPT)
    focus = mod_root / FOCUS_REL
    focus.parent.mkdir(parents=True)
    focus.write_text("focus = {\n\tid = OLD_b\n}\n", encoding="utf-8")

    result = upstream_analysis_shim.run(
        "rename_focus_ids",
        mod_root,
        {"focus_file": str(focus), "tag": "tst", "allowed_roots": [str(mod_root)]},
    )

    assert result["ok"] is True
    assert result["tag"] == "TST"
    assert result["focus_file"] == FOCUS_REL
    assert result["total"] == result["changed_total"] == 1
    assert result["returned"] == result["total"]
    assert result["files"][0]["root"] == "root"


# ---------------------------------------------------------------------------
# Real-checkout integration coverage
# ---------------------------------------------------------------------------


def _load_real_module(checkout: Path) -> ModuleType:
    script = checkout / "tools" / "standardization" / "rename_focus_ids.py"
    spec = importlib.util.spec_from_file_location("_md_mcp_test_rename_oracle", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_md_mcp_test_rename_oracle"] = module
    # Never leave __pycache__ files beside the loaded script (the real checkout
    # is review-only); the shim sets the same flag for its subprocess imports.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@pytest.mark.integration
def test_plan_matches_upstream_mutating_script_on_fixture_copy(
    real_mod_root: Path, tmp_path: Path
) -> None:
    """The plan bytes must equal what upstream's renamer writes to a tmp copy."""
    fixture = make_mod_root(tmp_path / "fixture")
    _build_fixture(fixture, crlf=True, bom=True)
    fixture_upstream = fixture / "tools" / "standardization"
    fixture_upstream.mkdir(parents=True)
    shutil.copyfile(
        real_mod_root / "tools" / "standardization" / "rename_focus_ids.py",
        fixture_upstream / "rename_focus_ids.py",
    )
    snapshot = {
        str(path): path.read_bytes() for path in sorted(fixture.rglob("*")) if path.is_file()
    }

    plan = rename_focus_ids_tool(fixture, FOCUS_REL, "TST", loc_file=LOC_REL)
    assert plan["ok"] is True

    oracle_root = make_mod_root(tmp_path / "oracle")
    shutil.copytree(fixture, oracle_root, dirs_exist_ok=True)
    oracle = _load_real_module(real_mod_root)
    mapping, changed = oracle.rename_focus_ids(
        oracle_root,
        oracle_root / FOCUS_REL,
        oracle_root / LOC_REL,
        "TST",
    )

    assert plan["mapping"] == mapping
    oracle_paths = {path.relative_to(oracle_root).as_posix() for path in changed}
    plan_paths = {entry["path"] for entry in plan["files"]}
    assert plan_paths == oracle_paths
    assert plan["focus_changed"] is True
    assert plan["loc_changed"] is True

    for entry in plan["files"]:
        preview = rename_focus_ids_tool(
            fixture, FOCUS_REL, "TST", loc_file=LOC_REL, path=entry["path"]
        )
        assert preview["ok"] is True
        emitted = preview["txt"].encode("utf-8")
        oracle_bytes = (oracle_root / entry["path"]).read_bytes()
        if entry["path"].endswith(".yml"):
            assert oracle_bytes == b"\xef\xbb\xbf" + emitted, entry["path"]
        else:
            assert oracle_bytes == emitted, entry["path"]
        expected = _count_oracle_replacements(fixture, entry["path"], plan["mapping"])
        assert entry["replacements"] == expected, entry["path"]

    assert {
        str(path): path.read_bytes() for path in sorted(fixture.rglob("*")) if path.is_file()
    } == snapshot

    # A no-BOM localisation file still changes, and upstream would write it back
    # with a UTF-8 BOM: the plan covers it and the preview bytes must match.
    oracle2_root = make_mod_root(tmp_path / "oracle2")
    shutil.copytree(fixture, oracle2_root, dirs_exist_ok=True)
    oracle.rename_focus_ids(oracle2_root, oracle2_root / FOCUS_REL, oracle2_root / LOC2_REL, "TST")
    plan2 = rename_focus_ids_tool(fixture, FOCUS_REL, "TST", loc_file=LOC2_REL)
    assert plan2["ok"] is True
    assert plan2["loc_file"] == LOC2_REL
    assert plan2["loc_changed"] is True
    assert LOC2_REL in {entry["path"] for entry in plan2["files"]}
    preview2 = rename_focus_ids_tool(fixture, FOCUS_REL, "TST", loc_file=LOC2_REL, path=LOC2_REL)
    assert preview2["ok"] is True
    assert preview2["changed"] is True
    assert "had_bom" not in preview2
    assert "write the file back with one" in preview2["note"]
    oracle2_bytes = (oracle2_root / LOC2_REL).read_bytes()
    assert oracle2_bytes == b"\xef\xbb\xbf" + preview2["txt"].encode("utf-8")


def _count_oracle_replacements(fixture: Path, rel: str, mapping: dict[str, str]) -> int:
    """Apply the real helpers the way the shim does to re-count replacements."""
    oracle = _load_real_module(fixture)
    counting = _CountingMapping(mapping)
    path = fixture / rel
    kind = "loc" if path.suffix == ".yml" else ""
    if not kind:
        kind = "focus" if rel == FOCUS_REL else "script"
    if kind == "loc":
        text = path.read_bytes().decode("utf-8-sig")
        oracle._replace_ids(text, counting, localization=True)
    else:
        raw_text = path.read_bytes().decode("utf-8")
        for line in raw_text.splitlines(keepends=True):
            oracle._replace_ids(line, counting, focus_file=kind == "focus")
    return counting.changed


def _tree_signatures(mod_root: Path, names: tuple[str, ...]) -> dict[str, tuple[int, int]]:
    """size/mtime signatures of every file and directory under `names`.

    Concurrent external edits can change signatures between snapshots; a
    failure lists the exact paths so accidental tool writes stay separable.
    """
    signatures: dict[str, tuple[int, int]] = {}
    for name in names:
        base = mod_root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            try:
                stat = path.stat()
            except OSError:
                continue
            signatures[path.relative_to(mod_root).as_posix()] = (
                stat.st_size,
                stat.st_mtime_ns,
            )
    return signatures


@pytest.mark.integration
def test_real_mod_rename_plan_and_preview_are_read_only(
    real_mod_root: Path, tmp_path: Path
) -> None:
    focus_rel = "common/national_focus/05_usa.txt"
    focus_path = real_mod_root / focus_rel
    before = focus_path.read_bytes()
    # Everything the tool can touch: the four scan trees plus the helper dirs,
    # so accidental writes or stray __pycache__ cannot hide in them.
    watched = ("common", "events", "history", "interface", "tools")
    before_signatures = _tree_signatures(real_mod_root, watched)

    plan = rename_focus_ids_tool(real_mod_root, focus_rel, "USA", limit=10)

    assert plan["ok"] is True
    assert plan["tag"] == "USA"
    assert plan["focus_file"] == focus_rel
    assert plan["mapping_count"] > 0
    assert plan["returned"] == min(10, plan["total"])
    assert len(json.dumps(plan, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
    for entry in plan["files"]:
        assert entry["root"] == "mod"
        assert entry["replacements"] > 0

    preview = rename_focus_ids_tool(real_mod_root, focus_rel, "USA", path=focus_rel)
    assert preview["ok"] is True
    assert preview["path"] == focus_rel
    assert preview["txt_bytes"] == len(before)
    assert preview["txt_truncated"] is (len(before) > MAX_TEXT_BYTES)
    assert before.decode("utf-8").startswith(preview["txt"])
    if preview["txt_truncated"]:
        assert "do NOT write clipped content back" in preview["note"]
    assert len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
    assert focus_path.read_bytes() == before
    assert _tree_signatures(real_mod_root, watched) == before_signatures


@pytest.mark.integration
def test_rename_focus_ids_over_real_stdio(real_mod_root: Path, tmp_path: Path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from tests.support.stdio import server_parameters

    focus_rel = "common/national_focus/05_usa.txt"
    focus_path = real_mod_root / focus_rel
    before = focus_path.read_bytes()
    watched = ("common", "events", "history", "interface", "tools")
    before_signatures = _tree_signatures(real_mod_root, watched)

    async def go() -> tuple[dict, dict]:
        async with (
            stdio_client(server_parameters(real_mod_root, tmp_path / "cache")) as (read, write),
            ClientSession(read, write, read_timeout_seconds=600.0) as session,
        ):
            await session.initialize()
            plan = await session.call_tool(
                "rename_focus_ids", {"focus_file": focus_rel, "tag": "USA", "limit": 5}
            )
            preview = await session.call_tool(
                "rename_focus_ids",
                {"focus_file": focus_rel, "tag": "USA", "path": focus_rel},
            )
            return (
                json.loads(cast(Any, plan).content[0].text),
                json.loads(cast(Any, preview).content[0].text),
            )

    plan, preview = asyncio.run(go())

    assert plan["ok"] is True
    assert plan["tag"] == "USA"
    assert plan["mapping_count"] > 0
    assert plan["returned"] == min(5, plan["total"])
    assert len(json.dumps(plan, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
    assert preview["ok"] is True
    assert preview["kind"] == "focus"
    assert preview["txt_bytes"] == len(before)
    assert preview["txt_truncated"] is True
    assert "do NOT write clipped content back" in preview["note"]
    assert len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
    assert focus_path.read_bytes() == before
    assert _tree_signatures(real_mod_root, watched) == before_signatures
