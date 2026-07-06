"""Tests for focus_layout — position resolution, collisions, chain errors."""

from __future__ import annotations

import inspect
from pathlib import Path

from md_mcp.analysis.focus_layout import focus_layout
from md_mcp.indexes import FocusIndex

_TREE = """focus_tree = {
    id = TST_tree
    focus = {
        id = TST_root
        x = 10
        y = 0
    }
    focus = {
        id = TST_child
        x = 2
        y = 1
        relative_position_id = TST_root
        prerequisite = { focus = TST_root }
    }
    focus = {
        id = TST_grandchild
        x = 0
        y = 1
        relative_position_id = TST_child
    }
    focus = {
        id = TST_overlap
        x = 12
        y = 1
    }
    focus = {
        id = TST_broken_rel
        x = 1
        y = 1
        relative_position_id = TST_nonexistent
    }
    focus = {
        id = TST_no_xy
        relative_position_id = TST_root
    }
}
"""


def _write_tree(root: Path, body: str = _TREE, name: str = "TST_tree.txt") -> str:
    d = root / "common" / "national_focus"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")
    return f"common/national_focus/{name}"


def test_signature():
    params = inspect.signature(focus_layout).parameters
    for p in ("tag", "file", "include_positions", "limit"):
        assert p in params


def test_requires_scope(tmp_path):
    out = focus_layout(tmp_path, None)
    assert out["ok"] is False


def test_file_scope_resolves_chains_and_collisions(tmp_path):
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel, include_positions=True)
    assert out["ok"] is True
    assert out["focus_count"] == 6

    pos = {p["id"]: (p["x"], p["y"]) for p in out["positions"]}
    assert pos["TST_root"] == (10, 0)
    assert pos["TST_child"] == (12, 1)
    assert pos["TST_grandchild"] == (12, 2)

    # TST_overlap sits at (12, 1) — same cell as resolved TST_child.
    assert out["collision_count"] == 1
    assert out["collisions"][0]["focuses"] == ["TST_child", "TST_overlap"]

    errors = {e["focus"]: e["error"] for e in out["chain_errors"]}
    assert errors["TST_broken_rel"] == "missing_relative"
    assert errors["TST_no_xy"] == "missing_xy"


def test_cyclic_relative_reported(tmp_path):
    body = """focus_tree = {
    focus = { id = TST_a x = 1 y = 1 relative_position_id = TST_b }
    focus = { id = TST_b x = 1 y = 1 relative_position_id = TST_a }
}
"""
    rel = _write_tree(tmp_path, body, name="TST_cycle.txt")
    out = focus_layout(tmp_path, None, file=rel)
    assert any(e["error"] == "cyclic_relative" for e in out["chain_errors"])
    assert out["resolved_count"] == 0


def test_tag_scope_via_index(tmp_path):
    _write_tree(tmp_path)
    idx = FocusIndex(tmp_path, tmp_path / ".cache", None)
    out = focus_layout(tmp_path, idx, tag="TST")
    assert out["ok"] is True
    assert out["scope"] == {"tag": "TST"}
    assert out["focus_count"] == 6
    assert out["bounding_box"]["min_y"] == 0


def test_positions_truncation(tmp_path):
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=2)
    assert out["positions_total"] == 4  # 6 focuses, 2 unresolvable
    assert len(out["positions"]) == 2
    assert out["positions_truncated"] is True


def test_missing_file_reported(tmp_path):
    out = focus_layout(tmp_path, None, file="common/national_focus/nope.txt")
    assert out["ok"] is True
    assert out["focus_count"] == 0
    assert out["parse_errors"][0]["error"] == "not found"
