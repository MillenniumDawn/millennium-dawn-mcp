"""find_references analysis tests."""

from __future__ import annotations

import pytest

from md_mcp.analysis.refs import find_references


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
    r = find_references(fake_mod_root, "bogus", "x")  # type: ignore[arg-type]
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
