"""Tests for focus-index-backed analysis tools."""

from __future__ import annotations

import json

from md_mcp.config import Settings
from md_mcp.indexes import FocusIndex
from md_mcp.tools import analysis_tools
from md_mcp.util.response import BUDGET_BYTES


def _settings(mod_root, cache_dir) -> Settings:
    return Settings(
        mod_root=mod_root,
        vanilla_path=None,
        cache_dir=cache_dir,
        validator_mode="in_process",
        default_lang="en",
    )


def _focus_text(focus_id: str) -> str:
    return f"""focus_tree = {{
    focus = {{
        id = {focus_id}
        x = 0
        y = 0
        prerequisite = {{ focus = TST_root }}
    }}
}}
"""


def test_focus_index_persists_parse_errors(fake_mod_root, cache_dir):
    broken = fake_mod_root / "common" / "national_focus" / "broken.txt"
    broken.write_text("focus_tree = { focus = { id = TST_broken x = {{{", encoding="utf-8")

    index = FocusIndex(fake_mod_root, cache_dir)
    index.ensure_fresh()
    errors = index.parse_errors()

    assert errors[0]["file"] == "common/national_focus/broken.txt"
    assert errors[0]["error"].startswith("parse failed:")
    assert index.resolve("TST_broken") is None

    reloaded = FocusIndex(fake_mod_root, cache_dir)
    assert reloaded.parse_errors() == errors


def test_find_focuses_reports_index_parse_failures(fake_mod_root, cache_dir):
    broken = fake_mod_root / "common" / "national_focus" / "broken.txt"
    broken.write_text("focus_tree = { focus = { id = TST_broken x = {{{", encoding="utf-8")

    out = analysis_tools.find_focuses_tool(
        _settings(fake_mod_root, cache_dir),
        FocusIndex(fake_mod_root, cache_dir),
        has_prereq="TST_root",
    )

    assert {match["id"] for match in out["matches"]} == {"TST_branch_a", "TST_branch_b"}
    assert out["partial"] is True
    assert out["skipped_files"] == 1
    assert out["skipped_records"] == 0
    assert out["partial_errors_total"] == 1
    assert out["partial_errors"][0]["file"] == "common/national_focus/broken.txt"
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_find_focuses_reports_index_records_missing_after_reparse(
    fake_mod_root, cache_dir, monkeypatch
):
    stale = fake_mod_root / "common" / "national_focus" / "stale.txt"
    stale.write_text(_focus_text("TST_stale"), encoding="utf-8")
    index = FocusIndex(fake_mod_root, cache_dir)
    index.ensure_fresh()
    stale.write_text(_focus_text("TST_replaced"), encoding="utf-8")
    monkeypatch.setattr(index._stale_check, "should_check", lambda: False)

    out = analysis_tools.find_focuses_tool(
        _settings(fake_mod_root, cache_dir), index, has_prereq="TST_root"
    )

    assert {match["id"] for match in out["matches"]} == {"TST_branch_a", "TST_branch_b"}
    assert out["partial"] is True
    assert out["skipped_files"] == 0
    assert out["skipped_records"] == 1
    assert out["partial_errors"] == [
        {
            "file": "common/national_focus/stale.txt",
            "error": "indexed records absent from reparsed AST",
            "count": 1,
            "sample_ids": ["TST_stale"],
            "sample_ids_truncated": False,
        }
    ]


def test_find_focuses_bounds_partial_errors(fake_mod_root, cache_dir, monkeypatch):
    focus_dir = fake_mod_root / "common" / "national_focus"
    for name in ("broken_a.txt", "broken_b.txt"):
        (focus_dir / name).write_text(
            "focus_tree = { focus = { id = TST_broken x = {{{", encoding="utf-8"
        )
    monkeypatch.setattr(analysis_tools, "_MAX_PARTIAL_ERRORS", 1)

    out = analysis_tools.find_focuses_tool(
        _settings(fake_mod_root, cache_dir),
        FocusIndex(fake_mod_root, cache_dir),
        has_prereq="TST_root",
    )

    assert out["partial_errors_total"] == 2
    assert len(out["partial_errors"]) == 1
    assert out["partial_errors_truncated"] is True
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
