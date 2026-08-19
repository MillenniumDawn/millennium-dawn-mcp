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
    assert out["collisions_total"] == 1
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


def test_collisions_and_chain_errors_report_totals_when_untruncated(tmp_path):
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel)
    assert out["collisions_total"] == 1
    assert len(out["collisions"]) == 1
    assert out["collisions_truncated"] is False
    assert out["chain_errors_total"] == 2
    assert len(out["chain_errors"]) == 2
    assert out["chain_errors_truncated"] is False


def test_collisions_truncation(tmp_path):
    pairs = "\n".join(
        f"    focus = {{ id = TST_c{i}{s} x = {i} y = 0 }}" for i in range(3) for s in ("a", "b")
    )
    body = f"focus_tree = {{\n{pairs}\n}}\n"
    rel = _write_tree(tmp_path, body, name="TST_stacked.txt")

    out = focus_layout(tmp_path, None, file=rel, limit=2)

    assert out["collisions_total"] == 3
    assert len(out["collisions"]) == 2
    assert out["collisions_truncated"] is True


def test_chain_errors_truncation(tmp_path):
    kids = "\n".join(
        f"    focus = {{ id = TST_k{i} x = 1 y = {i} relative_position_id = TST_ghost }}"
        for i in range(1, 5)
    )
    body = f"focus_tree = {{\n{kids}\n}}\n"
    rel = _write_tree(tmp_path, body, name="TST_ghosts.txt")

    out = focus_layout(tmp_path, None, file=rel, limit=2)

    assert out["chain_errors_total"] == 4
    assert len(out["chain_errors"]) == 2
    assert out["chain_errors_truncated"] is True


def test_missing_file_reported(tmp_path):
    out = focus_layout(tmp_path, None, file="common/national_focus/nope.txt")
    assert out["ok"] is True
    assert out["focus_count"] == 0
    assert out["parse_errors"][0]["error"] == "not found"


class _StubIndex:
    """Stands in for FocusIndex.files_for_tag without building a real index."""

    def __init__(self, files):
        self._files = files

    def files_for_tag(self, tag):
        return self._files


def test_duplicate_id_does_not_collide_with_itself(tmp_path):
    """A focus defined in two scope files must not be reported as its own collision."""
    body = "focus_tree = {\n    focus = { id = TST_dup x = 1 y = 1 }\n}\n"
    _write_tree(tmp_path, body, name="TST_one.txt")
    _write_tree(tmp_path, body, name="TST_two.txt")
    idx = _StubIndex(["common/national_focus/TST_one.txt", "common/national_focus/TST_two.txt"])

    out = focus_layout(tmp_path, idx, tag="TST", include_positions=True)

    assert out["focus_count"] == 1
    assert out["collisions_total"] == 0
    assert out["positions_total"] == 1
    # The duplicate definition is still surfaced, just not as a collision.
    assert out["duplicate_definitions"] == [
        {
            "id": "TST_dup",
            "files": ["common/national_focus/TST_one.txt", "common/national_focus/TST_two.txt"],
        }
    ]


def test_broken_parent_reported_once_not_per_descendant(tmp_path):
    """One unresolvable parent must yield one chain error, not one per dependent."""
    kids = "\n".join(
        f"    focus = {{ id = TST_kid{i} x = 1 y = {i} relative_position_id = TST_parent }}"
        for i in range(1, 7)
    )
    body = f"focus_tree = {{\n    focus = {{ id = TST_parent }}\n{kids}\n}}\n"
    rel = _write_tree(tmp_path, body, name="TST_hub.txt")

    out = focus_layout(tmp_path, None, file=rel)

    missing_xy = [e for e in out["chain_errors"] if e["error"] == "missing_xy"]
    assert missing_xy == [
        {"focus": "TST_parent", "error": "missing_xy", "file": "common/national_focus/TST_hub.txt"}
    ]
    assert out["resolved_count"] == 0


def test_missing_relative_still_reported_per_referrer(tmp_path):
    """Each focus pointing at an absent id has its own broken link — keep them all."""
    kids = "\n".join(
        f"    focus = {{ id = TST_k{i} x = 1 y = {i} relative_position_id = TST_ghost }}"
        for i in range(1, 4)
    )
    body = f"focus_tree = {{\n{kids}\n}}\n"
    rel = _write_tree(tmp_path, body, name="TST_ghost.txt")

    out = focus_layout(tmp_path, None, file=rel)

    missing = sorted(e["focus"] for e in out["chain_errors"] if e["error"] == "missing_relative")
    assert missing == ["TST_k1", "TST_k2", "TST_k3"]


def test_negative_limit_clamped_to_empty_pages(tmp_path):
    """Negative limit must clamp to 0 for all three paginated lists (bug #6)."""
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=-5)
    assert out["positions"] == []
    assert out["positions_total"] == 4
    assert out["positions_truncated"] is True
    assert out["collisions"] == []
    assert out["collisions_total"] == 1
    assert out["collisions_truncated"] is True
    assert out["chain_errors"] == []
    assert out["chain_errors_total"] == 2
    assert out["chain_errors_truncated"] is True


def test_zero_limit_yields_empty_pages(tmp_path):
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=0)
    assert out["positions"] == []
    assert out["positions_truncated"] is True
    assert out["collisions"] == []
    assert out["collisions_truncated"] is True
    assert out["chain_errors"] == []
    assert out["chain_errors_truncated"] is True


def test_limit_at_total_not_truncated(tmp_path):
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=4)
    assert len(out["positions"]) == 4
    assert out["positions_truncated"] is False
    assert out["positions_total"] == 4


def test_limit_one_below_total_truncated(tmp_path):
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=3)
    assert len(out["positions"]) == 3
    assert out["positions_truncated"] is True


def test_negative_limit_without_positions_still_clamps(tmp_path):
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel, include_positions=False, limit=-1)
    assert "positions" not in out
    assert "positions_total" not in out
    assert "positions_truncated" not in out
    assert out["collisions"] == []
    assert out["chain_errors"] == []
