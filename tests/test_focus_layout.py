"""Tests for focus_layout — position resolution, collisions, chain errors."""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import sys
import time
from importlib.util import find_spec
from pathlib import Path
from typing import cast

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from md_mcp.analysis.focus_layout import focus_layout
from md_mcp.analysis.scope import iter_scope_files
from md_mcp.config import load as load_settings
from md_mcp.indexes import FocusIndex
from md_mcp.util.response import BUDGET_BYTES
from tests.support.stdio import server_parameters

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"

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


def test_requires_scope(tmp_path):
    out = focus_layout(tmp_path, None)
    assert out["ok"] is False


def test_tag_scope_requires_the_focus_index(tmp_path):
    out = focus_layout(tmp_path, None, tag="TST")
    assert out == {"ok": False, "error": "tag= scope requires the focus index."}


def test_tag_scope_reports_only_matching_focus_ids_in_selected_trees(tmp_path):
    body = """focus_tree = {
    id = MIXED_tree
    focus = { id = TST_visible x = 1 y = 0 }
    focus = { id = OTHER_unreported x = 2 y = 0 }
}
"""
    rel = _write_tree(tmp_path, body, name="TST_mixed.txt")
    index = _StubIndex([rel])

    out = focus_layout(tmp_path, index, tag="TST", include_positions=True)

    assert out["focus_count"] == 1
    assert [position["id"] for position in out["positions"]] == ["TST_visible"]
    assert out["bounding_box"] == {"min_x": 1, "max_x": 1, "min_y": 0, "max_y": 0}


def test_yes_or_no_is_not_a_coordinate(tmp_path):
    body = "focus_tree = {\n    focus = { id = TST_flag x = yes y = 1 }\n}\n"
    rel = _write_tree(tmp_path, body, name="TST_flag.txt")

    out = focus_layout(tmp_path, None, file=rel)

    assert out["resolved_count"] == 0
    assert out["chain_errors"] == [
        {
            "focus": "TST_flag",
            "error": "missing_xy",
            "file": rel,
            "line": 2,
            "tree": "tree at line 1",
            "tree_line": 1,
        }
    ]


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


def test_missing_shared_import_is_reported_with_tree_provenance(tmp_path):
    rel = _write_tree(
        tmp_path,
        """focus_tree = {
    id = TST_missing_consumer
    shared_focus = TST_missing_shared
    focus = { id = TST_child x = 1 y = 0 }
}
""",
        name="TST_missing_consumer.txt",
    )

    out = focus_layout(tmp_path, None, file=rel, limit=1)

    assert out["chain_errors_total"] == 1
    assert out["chain_errors"] == [
        {
            "focus": "TST_missing_shared",
            "error": "missing_shared",
            "ref": "TST_missing_shared",
            "file": rel,
            "line": 1,
            "tree": "TST_missing_consumer",
            "tree_line": 1,
        }
    ]


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


@pytest.mark.parametrize(
    ("limit", "positions", "collisions", "chain_errors"),
    [
        (2, 2, 1, 2),
        ("2", 2, 1, 2),
        (2.9, 2, 1, 2),
        (3, 3, 1, 2),
        (4, 4, 1, 2),
        (100, 4, 1, 2),
        (None, 4, 1, 2),
        (1, 1, 1, 1),
        (0, 0, 0, 0),
        (-5, 0, 0, 0),
    ],
)
def test_limit_pages_each_list_and_reports_its_total(
    tmp_path, limit, positions, collisions, chain_errors
):
    rel = _write_tree(tmp_path)

    out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=limit)

    assert out["ok"] is True
    # _TREE has six focuses, two unresolvable, one collision, two chain errors.
    for key, returned, total in (
        ("positions", positions, 4),
        ("collisions", collisions, 1),
        ("chain_errors", chain_errors, 2),
    ):
        assert len(out[key]) == returned
        assert out[f"{key}_total"] == total
        assert out[f"{key}_truncated"] is (returned < total)


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


def test_file_scope_without_imports_does_not_initialize_or_scan_index(tmp_path, monkeypatch):
    requested = _write_tree(
        tmp_path,
        "focus_tree = { focus = { id = TST_local x = 1 y = 0 } }\n",
        name="TST_requested.txt",
    )
    for number in range(10):
        _write_tree(
            tmp_path,
            f"focus_tree = {{ focus = {{ id = OTHER_{number} x = 1 y = 0 }} }}\n",
            name=f"OTHER_{number}.txt",
        )

    class _IndexMustStayCold:
        def files_for_tag(self, tag: str) -> list[str]:
            return []

        def list_files(self):
            pytest.fail("no-import file scope must not initialize the focus index")

        def records_for_file(self, _relpath):
            pytest.fail("no-import file scope must not inspect index metadata")

    module = importlib.import_module("md_mcp.analysis.focus_layout")
    parsed_files = []
    parse_layout = module.layout_file

    def count_layout_files(relpath, root, source):
        parsed_files.append(relpath)
        return parse_layout(relpath, root, source)

    monkeypatch.setattr(module, "layout_file", count_layout_files)
    out = focus_layout(tmp_path, _IndexMustStayCold(), file=requested)

    assert parsed_files == [requested]
    assert out["files_scanned"] == 1


def test_index_metadata_limits_import_parsing_to_shared_files(tmp_path, monkeypatch):
    requested = _write_tree(
        tmp_path,
        """focus_tree = {
    id = TST_consumer
    shared_focus = TST_shared
    focus = { id = TST_child x = 1 y = 0 relative_position_id = TST_shared }
}
""",
        name="TST_consumer.txt",
    )
    shared = "common/national_focus/00_shared.txt"
    unrelated = _write_tree(
        tmp_path,
        "focus_tree = { focus = { id = OTH_unrelated x = 20 y = 0 } }\n",
        name="OTH_unrelated.txt",
    )
    _write_tree(
        tmp_path,
        "shared_focus = { id = TST_shared x = 2 y = 0 }\n",
        name="00_shared.txt",
    )

    class _IndexedFiles:
        def files_for_tag(self, tag: str) -> list[str]:
            return [requested]

        def list_files(self):
            return [requested, shared, unrelated]

        def records_for_file(self, relpath):
            if relpath == shared:
                return [{"id": "TST_shared", "kind": "shared_focus"}]
            if relpath == requested:
                return [{"id": "TST_consumer", "kind": "focus_tree"}]
            return [{"id": "OTH_unrelated", "kind": "focus_tree"}]

    module = importlib.import_module("md_mcp.analysis.focus_layout")
    parsed_files = []
    parse_layout = module.layout_file

    def count_layout_files(relpath, root, source):
        parsed_files.append(relpath)
        return parse_layout(relpath, root, source)

    monkeypatch.setattr(module, "layout_file", count_layout_files)
    out = focus_layout(tmp_path, _IndexedFiles(), file=requested, include_positions=True)

    assert set(parsed_files) == {requested, shared}
    assert unrelated not in parsed_files
    assert out["focus_count"] == 2
    assert next(p for p in out["positions"] if p["id"] == "TST_child")["x"] == 3


def test_missing_file_reported(tmp_path):
    out = focus_layout(tmp_path, None, file="common/national_focus/nope.txt")
    assert out["ok"] is True
    assert out["focus_count"] == 0
    assert out["parse_errors"][0]["error"] == "not found"


def test_limit_uncoercible_returns_error_not_raise(tmp_path):
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel, limit="not-a-number")
    assert out["ok"] is False
    assert "limit" in out["error"]
    assert out["error"] == "limit must be an integer, got 'not-a-number'"


def test_limit_string_matches_int_output(tmp_path):
    rel = _write_tree(tmp_path)
    string_out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit="5")
    int_out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=5)
    assert string_out == int_out


class _StubIndex:
    """Stands in for FocusIndex.files_for_tag without building a real index."""

    def __init__(self, files):
        self._files = files

    def files_for_tag(self, tag):
        return self._files


def _write_submod_tree(root: Path, name: str, focus_id: str, x: int) -> str:
    d = root / "common" / "national_focus"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"focus_tree = {{\n    focus = {{ id = {focus_id} x = {x} y = 0 }}\n}}\n",
        encoding="utf-8",
    )
    return f"common/national_focus/{name}"


def test_file_scope_prefers_submod_and_keeps_base_only_files(tmp_path):
    base = tmp_path / "base"
    overlay = tmp_path / "overlay"
    shadowed = "TST_shadowed.txt"
    shadowed_rel = _write_submod_tree(base, shadowed, "TST_base_shadowed", 1)
    _write_submod_tree(overlay, shadowed, "TST_overlay_shadowed", 10)
    overlay_only_rel = _write_submod_tree(overlay, "TST_overlay_only.txt", "TST_overlay_only", 20)
    base_only_rel = _write_submod_tree(base, "TST_base_only.txt", "TST_base_only", 30)

    shadowed_out = focus_layout(
        base, None, file=shadowed_rel, include_positions=True, submod_root=overlay
    )
    overlay_only_out = focus_layout(
        base, None, file=overlay_only_rel, include_positions=True, submod_root=overlay
    )
    base_only_out = focus_layout(
        base, None, file=base_only_rel, include_positions=True, submod_root=overlay
    )

    assert [p["id"] for p in shadowed_out["positions"]] == ["TST_overlay_shadowed"]
    assert shadowed_out["positions"][0]["x"] == 10
    assert [p["id"] for p in overlay_only_out["positions"]] == ["TST_overlay_only"]
    assert "parse_errors" not in overlay_only_out
    assert [p["id"] for p in base_only_out["positions"]] == ["TST_base_only"]
    assert "parse_errors" not in base_only_out


def test_file_scope_imports_shared_focus_from_effective_cross_file_overlay(tmp_path):
    base = tmp_path / "base"
    overlay = tmp_path / "overlay"
    shared_rel = "common/national_focus/00_shared.txt"
    for root, x in ((base, 4), (overlay, 9)):
        directory = root / "common" / "national_focus"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "00_shared.txt").write_text(
            f"shared_focus = {{ id = TST_shared_anchor x = {x} y = 0 }}\n",
            encoding="utf-8",
        )
    tree_rel = _write_tree(
        base,
        """focus_tree = {
    id = TST_consumer
    shared_focus = TST_shared_anchor
    focus = { id = TST_consumer_child x = 1 y = 0 relative_position_id = TST_shared_anchor }
}
""",
        name="TST_consumer.txt",
    )

    out = focus_layout(
        base,
        None,
        file=tree_rel,
        include_positions=True,
        submod_root=overlay,
    )

    by_id = {position["id"]: position for position in out["positions"]}
    assert out["focus_count"] == 2
    assert by_id["TST_shared_anchor"]["x"] == 9
    assert by_id["TST_shared_anchor"]["file"] == shared_rel
    assert by_id["TST_consumer_child"]["x"] == 10


def test_tag_scope_uses_submod_overlay_with_base_fallback(tmp_path):
    base = tmp_path / "base"
    overlay = tmp_path / "overlay"
    cache = tmp_path / "cache"
    _write_submod_tree(base, "TST_shadowed.txt", "TST_base_shadowed", 1)
    _write_submod_tree(overlay, "TST_shadowed.txt", "TST_overlay_shadowed", 10)
    _write_submod_tree(overlay, "TST_overlay_only.txt", "TST_overlay_only", 20)
    _write_submod_tree(base, "TST_base_only.txt", "TST_base_only", 30)
    index = FocusIndex(base, cache, None, submod_root=overlay)

    out = focus_layout(base, index, tag="TST", include_positions=True, submod_root=overlay)

    positions = {p["id"]: p["x"] for p in out["positions"]}
    assert out["files_scanned"] == 3
    assert positions == {
        "TST_base_only": 30,
        "TST_overlay_only": 20,
        "TST_overlay_shadowed": 10,
    }
    assert "TST_base_shadowed" not in positions


def test_duplicate_id_does_not_collide_with_itself(tmp_path):
    """A focus defined in two scope files must not be reported as its own collision."""
    body = "focus_tree = {\n    focus = { id = TST_dup x = 1 y = 1 }\n}\n"
    _write_tree(tmp_path, body, name="TST_one.txt")
    _write_tree(tmp_path, body, name="TST_two.txt")
    idx = _StubIndex(["common/national_focus/TST_one.txt", "common/national_focus/TST_two.txt"])

    out = focus_layout(tmp_path, idx, tag="TST", include_positions=True)

    assert out["focus_count"] == 2
    assert out["collisions_total"] == 0
    assert out["positions_total"] == 2
    # Separate tree contexts resolve independently, with provenance.
    assert [position["file"] for position in out["positions"]] == [
        "common/national_focus/TST_one.txt",
        "common/national_focus/TST_two.txt",
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
    assert len(missing_xy) == 1
    assert missing_xy[0]["focus"] == "TST_parent"
    assert missing_xy[0]["file"] == "common/national_focus/TST_hub.txt"
    assert missing_xy[0]["line"] == 2
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


def test_duplicate_definitions_are_paginated_with_totals(tmp_path):
    declarations = "\n".join(
        f"    focus = {{ id = TST_dup{i} x = 1 y = 0 }}\n"
        f"    focus = {{ id = TST_dup{i} x = 2 y = 0 }}"
        for i in range(3)
    )
    rel = _write_tree(
        tmp_path,
        f"focus_tree = {{\n{declarations}\n}}\n",
        name="TST_duplicates.txt",
    )

    first = focus_layout(tmp_path, None, file=rel, limit=1)
    second = focus_layout(tmp_path, None, file=rel, limit=1, offset=1)

    assert first["duplicate_definitions_total"] == 3
    assert first["duplicate_definitions_truncated"] is True
    assert len(first["duplicate_definitions"]) == 1
    assert second["duplicate_definitions"][0]["id"] != first["duplicate_definitions"][0]["id"]
    assert second["duplicate_definitions_total"] == 3


def test_accumulated_coordinate_overflow_is_unresolved(tmp_path):
    huge_finite = "1" + "0" * 308
    body = f"""focus_tree = {{
    focus = {{ id = TST_finite_root x = {huge_finite} y = 0 }}
    focus = {{ id = TST_overflow x = {huge_finite} y = 0 relative_position_id = TST_finite_root }}
}}
"""
    rel = _write_tree(tmp_path, body, name="TST_overflow.txt")

    out = focus_layout(tmp_path, None, file=rel, include_positions=True)

    assert any(error["error"] == "nonfinite_xy" for error in out["chain_errors"]), out
    assert out["resolved_count"] == 1
    assert out["positions_total"] == 1
    assert all(math.isfinite(position["x"]) for position in out["positions"])
    assert out["bounding_box"]["max_x"] == float(huge_finite)


def test_negative_limit_without_positions_still_clamps(tmp_path):
    rel = _write_tree(tmp_path)
    out = focus_layout(tmp_path, None, file=rel, include_positions=False, limit=-1)
    assert "positions" not in out
    assert "positions_total" not in out
    assert "positions_truncated" not in out
    assert out["collisions"] == []
    assert out["collisions_total"] == 1
    assert out["collisions_truncated"] is True
    assert out["chain_errors"] == []
    assert out["chain_errors_total"] == 2
    assert out["chain_errors_truncated"] is True


def test_negative_limit_empty_collisions_not_truncated(tmp_path):
    body = "focus_tree = {\n    focus = { id = TST_only x = 1 y = 1 }\n}\n"
    rel = _write_tree(tmp_path, body, name="TST_single.txt")
    out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=-1)
    assert out["positions"] == []
    assert out["positions_total"] == 1
    assert out["positions_truncated"] is True
    assert out["collisions"] == []
    assert out["collisions_total"] == 0
    assert out["collisions_truncated"] is False
    assert out["chain_errors"] == []
    assert out["chain_errors_total"] == 0
    assert out["chain_errors_truncated"] is False


def test_enforce_budget_drops_positions_when_over(tmp_path):
    unique = "\n".join(f"    focus = {{ id = TST_f{i:04d} x = {i} y = 0 }}" for i in range(2500))
    stacked = "\n".join(
        f"    focus = {{ id = TST_c{i:02d}{s} x = {i} y = 1 }}"
        for i in range(10)
        for s in ("a", "b")
    )
    body = f"focus_tree = {{\n{unique}\n{stacked}\n}}\n"
    rel = _write_tree(tmp_path, body, name="TST_budget.txt")

    out = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=10000)

    assert out.get("size_truncated") is True
    assert "positions" not in out
    assert out["positions_total"] == 2520
    assert out["positions_dropped"] == 2520
    assert "collisions" in out
    assert "collisions_dropped" not in out
    assert out["collisions_total"] == 10
    assert "chain_errors" in out
    assert "chain_errors_dropped" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_fractional_coordinates_and_offset_pagination(tmp_path):
    body = """focus_tree = {
    id = TST_fractional
    focus = { id = TST_root x = 1.25 y = 0 }
    focus = { id = TST_child x = 0.5 y = 0 relative_position_id = TST_root }
    focus = { id = TST_close x = 2.25 y = 0 }
}
"""
    rel = _write_tree(tmp_path, body, name="TST_fractional.txt")

    first = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=1)
    second = focus_layout(tmp_path, None, file=rel, include_positions=True, limit=1, offset=1)

    assert first["positions"][0]["x"] == 1.25
    assert second["positions"][0]["id"] == "TST_child"
    # Brazil regression: statically visible same-row focus slots need two columns.
    warnings = focus_layout(tmp_path, None, file=rel)["spacing_warnings"]
    assert any(item.get("other_x") == 1.75 for item in warnings)


def test_decimal_coordinates_add_and_space_exactly(tmp_path):
    body = """focus_tree = {
    focus = { id = TST_a x = 0.1 y = 0 }
    focus = { id = TST_b x = 0.2 y = 1 relative_position_id = TST_a }
    focus = { id = TST_c x = 0.3 y = 1 }
    focus = { id = TST_d x = 2.3 y = 1 }
}
"""
    rel = _write_tree(tmp_path, body, name="TST_decimal.txt")

    out = focus_layout(tmp_path, None, file=rel, include_positions=True)

    assert [item["focuses"] for item in out["collisions"]] == [["TST_b", "TST_c"]]
    assert out["spacing_warnings_total"] == 0
    assert next(item for item in out["positions"] if item["id"] == "TST_b")["x"] == 0.3


def test_file_scope_normalizes_dot_and_absolute_spellings(tmp_path):
    body = """shared_focus = { id = SHR_x x = 1 y = 0 }
focus_tree = {
    id = TST_mixed
    shared_focus = SHR_x
    focus = { id = TST_a x = 5 y = 0 }
}
"""
    rel = _write_tree(tmp_path, body, name="TST_mixed.txt")
    expected = focus_layout(tmp_path, None, file=rel, include_positions=True)

    for spelling in (f"./{rel}", str(tmp_path / rel)):
        out = focus_layout(tmp_path, None, file=spelling, include_positions=True)
        assert out["chain_errors"] == expected["chain_errors"] == []
        assert out["positions"] == expected["positions"]
        assert out["files_scanned"] == 1


def test_tag_scope_checks_imported_shared_focuses(tmp_path):
    shared = """shared_focus = { id = SHR_x x = 5 y = 0 }
shared_focus = { id = SHR_y x = 9 y = 0 }
shared_focus = { id = SHR_z x = 20 y = 0 }
shared_focus = { id = SHR_w x = 20 y = 0 }
"""
    body = """focus_tree = {
    id = TST_tree
    shared_focus = SHR_x
    shared_focus = SHR_y
    shared_focus = SHR_z
    shared_focus = SHR_w
    focus = { id = TST_a x = 5 y = 0 }
    focus = { id = TST_b x = 8 y = 0 }
}
"""
    _write_tree(tmp_path, shared, name="SHR_shared.txt")
    rel = _write_tree(tmp_path, body, name="TST_tree.txt")

    by_tag = focus_layout(tmp_path, _StubIndex([rel]), tag="TST")
    by_file = focus_layout(tmp_path, None, file=rel)

    assert [item["focuses"] for item in by_tag["collisions"]] == [["SHR_x", "TST_a"]]
    assert [item["focuses"] for item in by_tag["spacing_warnings"]] == [["SHR_y", "TST_b"]]
    assert [item["focuses"] for item in by_file["collisions"]] == [
        ["SHR_x", "TST_a"],
        ["SHR_w", "SHR_z"],
    ]


def test_tree_identity_shared_imports_dynamic_descendants_and_long_chains(tmp_path):
    shared = """shared_focus = {
    id = TST_shared
    x = 2.5
    y = 0
    offset = { x = { factor = 1 } }
}
joint_focus = { id = TST_joint x = 1 y = 1 prerequisite = { focus = TST_shared } }
"""
    body = """focus_tree = {
    id = TST_left
    shared_focus = TST_shared
    focus = { id = TST_dynamic_child x = 1 y = 0 relative_position_id = TST_shared }
}
focus_tree = {
    id = TST_right
    focus = { id = TST_shared x = 50 y = 0 }
}
"""
    _write_tree(tmp_path, shared, name="TST_shared.txt")
    _write_tree(tmp_path, body, name="TST_trees.txt")
    index = _StubIndex(
        [
            "common/national_focus/TST_shared.txt",
            "common/national_focus/TST_trees.txt",
        ]
    )
    out = focus_layout(tmp_path, index, tag="TST", include_positions=True)
    imported = next(
        item
        for item in out["positions"]
        if item["id"] == "TST_shared" and item["tree"] == "TST_left"
    )
    child = next(item for item in out["positions"] if item["id"] == "TST_dynamic_child")
    joint = next(item for item in out["positions"] if item["id"] == "TST_joint")
    assert imported["x"] == 2.5
    assert imported["dynamic"] is True
    assert child["dynamic"] is True
    assert joint["tree"] == "TST_left"
    assert any(
        item["id"] == "TST_shared" and item["tree"] == "TST_right" for item in out["positions"]
    )

    chain = "\n".join(
        f"    focus = {{ id = TST_n{i} x = 0.5 y = 0 relative_position_id = TST_n{i - 1} }}"
        for i in range(1, 1200)
    )
    long_rel = _write_tree(
        tmp_path,
        f"focus_tree = {{\n    focus = {{ id = TST_n0 x = 1.5 y = 0 }}\n{chain}\n}}\n",
        name="TST_long.txt",
    )
    long_out = focus_layout(tmp_path, None, file=long_rel, include_positions=True, limit=1)
    assert long_out["resolved_count"] == 1200
    assert long_out["positions_total"] == 1200


def test_nonfinite_and_malformed_coordinates_are_unresolved(tmp_path):
    too_large = "1" + "0" * 400
    body = f"""focus_tree = {{
    focus = {{ id = TST_infinite x = nan y = 1 }}
    focus = {{ id = TST_malformed x = {{ factor = 2 }} y = 1 }}
    focus = {{ id = TST_too_large x = {too_large} y = 1 }}
}}
"""
    rel = _write_tree(tmp_path, body, name="TST_invalid_coords.txt")

    out = focus_layout(tmp_path, None, file=rel)

    assert out["resolved_count"] == 0
    assert {error["focus"] for error in out["chain_errors"]} == {
        "TST_infinite",
        "TST_malformed",
        "TST_too_large",
    }


@pytest.mark.integration
def test_focus_layout_live_stdio_and_brazil_matches_upstream_helper(
    real_mod_root, tmp_path, monkeypatch, record_property
):
    brazil_file = "common/national_focus/05_brazil.txt"
    no_import_file = "common/national_focus/05_afghanistan.txt"
    cache_dir = tmp_path / "mcp-cache"
    source_root = SOURCE_ROOT
    module_spec = find_spec("md_mcp.analysis.focus_layout")
    assert module_spec is not None and module_spec.origin is not None
    module_source = Path(module_spec.origin).resolve()
    assert module_source == (source_root / "md_mcp" / "analysis" / "focus_layout.py").resolve()
    parameters = server_parameters(real_mod_root, cache_dir)

    async def probe_stdio():
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write, read_timeout_seconds=600) as session,
        ):
            await session.initialize()

            async def call_layout(path, include_positions=False):
                arguments = {"file": path}
                if include_positions:
                    arguments.update(include_positions=True, limit=10000)
                response = await session.call_tool("focus_layout", arguments)
                assert not response.is_error
                content = cast(TextContent, response.content[0])
                assert content.type == "text"
                return json.loads(content.text)

            started = time.perf_counter()
            no_import_cold = await call_layout(no_import_file)
            no_import_cold_seconds = time.perf_counter() - started
            started = time.perf_counter()
            no_import_warm = await call_layout(no_import_file)
            no_import_warm_seconds = time.perf_counter() - started
            started = time.perf_counter()
            brazil_cold = await call_layout(brazil_file, include_positions=True)
            brazil_cold_seconds = time.perf_counter() - started
            started = time.perf_counter()
            brazil_warm = await call_layout(brazil_file, include_positions=True)
            brazil_warm_seconds = time.perf_counter() - started
            return (
                no_import_cold,
                no_import_warm,
                brazil_cold,
                brazil_warm,
                no_import_cold_seconds,
                no_import_warm_seconds,
                brazil_cold_seconds,
                brazil_warm_seconds,
            )

    (
        no_import_cold,
        no_import_warm,
        brazil_cold,
        brazil_warm,
        no_import_cold_seconds,
        no_import_warm_seconds,
        brazil_cold_seconds,
        brazil_warm_seconds,
    ) = asyncio.run(probe_stdio())
    assert no_import_cold["files_scanned"] == 1
    assert no_import_warm["files_scanned"] == 1
    assert brazil_cold["files_scanned"] > 1
    assert brazil_warm["files_scanned"] == brazil_cold["files_scanned"]

    settings = load_settings(str(real_mod_root))
    baseline_index = FocusIndex(
        settings.mod_root,
        tmp_path / "baseline-index",
        settings.vanilla_path,
        submod_root=settings.submod_root,
    )
    whole_corpus_files = baseline_index.list_files()
    whole_corpus_parse_errors: list[dict] = []
    whole_corpus_started = time.perf_counter()
    whole_corpus_parsed = list(
        iter_scope_files(
            whole_corpus_files,
            settings.mod_root,
            settings.vanilla_path,
            whole_corpus_parse_errors,
            submod_root=settings.submod_root,
        )
    )
    whole_corpus_parse_count = len(whole_corpus_parsed)
    whole_corpus_seconds = time.perf_counter() - whole_corpus_started

    validation_dir = real_mod_root / "tools" / "validation"
    monkeypatch.syspath_prepend(str(validation_dir))
    monkeypatch.setenv("MD_NO_CACHE", "1")
    upstream_validator = importlib.import_module("validate_focus_tree")
    validator = upstream_validator.Validator(
        str(real_mod_root), staged_only=False, workers=1, no_cache=True
    )
    upstream_started = time.perf_counter()
    scans = validator._focus_scans()
    # A reportable set selects every importing tree, so keep only Brazil's trees.
    brazil_only = [
        layout if layout["filepath"] == brazil_file else {**layout, "trees": []}
        for layout in (scan["layout"] for scan in scans)
    ]
    upstream_layout = upstream_validator.analyze_layout(brazil_only)
    upstream_seconds = time.perf_counter() - upstream_started

    same_cell_pairs = sum(
        len(collision["focuses"]) * (len(collision["focuses"]) - 1) // 2
        for collision in brazil_cold["collisions"]
    )
    mcp_overlap_pairs = same_cell_pairs + brazil_cold["spacing_warnings_total"]
    upstream_overlap_pairs = upstream_layout["counts"]["overlap_pairs"]
    assert mcp_overlap_pairs == upstream_overlap_pairs

    geometry = importlib.import_module("focus_geometry")
    shared_index, dependents, requirements = geometry._shared_index(
        [scan["layout"] for scan in scans]
    )
    upstream_positions = {}
    for source in (scan["layout"] for scan in scans):
        if source["filepath"] != brazil_file:
            continue
        for tree in source["trees"]:
            focuses, _missing = geometry._assemble(tree, shared_index, dependents, requirements)
            by_id: dict[str, list[dict]] = {}
            for focus in focuses:
                by_id.setdefault(focus["id"], []).append(focus)
            tree_positions, _failures = geometry._positions(by_id)
            for focus_id, position in tree_positions.items():
                if position is not None:
                    upstream_positions[(tree["id"], focus_id)] = (
                        float(position[0]),
                        float(position[1]),
                    )
    mcp_positions = {
        (position["tree"], position["id"]): (
            float(position["x"]),
            float(position["y"]),
        )
        for position in brazil_cold["positions"]
    }
    assert mcp_positions == upstream_positions

    metrics = {
        "reference_kind": "rejected-whole-corpus-path-replay",
        "original_main_baseline_measured": False,
        "rejected_whole_corpus_parse_count": whole_corpus_parse_count,
        "rejected_whole_corpus_seconds": whole_corpus_seconds,
        "no_import_cold_seconds": no_import_cold_seconds,
        "no_import_warm_seconds": no_import_warm_seconds,
        "no_import_parse_count_cold_warm": [
            no_import_cold["files_scanned"],
            no_import_warm["files_scanned"],
        ],
        "brazil_import_cold_seconds": brazil_cold_seconds,
        "brazil_import_warm_seconds": brazil_warm_seconds,
        "brazil_import_parse_count_cold_warm": [
            brazil_cold["files_scanned"],
            brazil_warm["files_scanned"],
        ],
        "upstream_focus_scan_count": len(scans),
        "upstream_seconds": upstream_seconds,
        "mcp_brazil_overlap_pairs": mcp_overlap_pairs,
        "upstream_brazil_overlap_pairs": upstream_overlap_pairs,
        "mcp_brazil_positions": len(mcp_positions),
        "upstream_brazil_positions": len(upstream_positions),
    }
    for key, value in metrics.items():
        record_property(key, value)
    sys.stdout.write(f"FOCUS_LAYOUT_LIVE_PROBE {json.dumps(metrics)}\n")
