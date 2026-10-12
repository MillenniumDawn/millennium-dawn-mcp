"""Resolver regression tests — focus anchoring against the index record (issue #153).

The focus index resolves duplicate ids last-write-wins, so `resolve_focus_tool` must
re-anchor freshly parsed records to the line the index picked instead of taking the
first match in the file. Anchoring rules come from `util/index_anchor.py`, shared
with the `md://` resources.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from md_mcp.config import Settings
from md_mcp.indexes import FocusIndex
from md_mcp.resources import focus_resource
from md_mcp.tools.resolver_tools import resolve_focus_tool
from md_mcp.util.index_anchor import IndexAnchorError
from tests.support.mod import mod_settings


def _focus_mod(tmp_path: Path, *texts: str) -> tuple[Settings, FocusIndex]:
    """Write one `common/national_focus/<n>.txt` per given text and build the index."""
    mod_root = tmp_path / "Mod"
    focus_dir = mod_root / "common" / "national_focus"
    focus_dir.mkdir(parents=True)
    for i, text in enumerate(texts):
        (focus_dir / f"focus_{i}.txt").write_text(text, encoding="utf-8")
    settings = mod_settings(mod_root, tmp_path / ".md-mcp-cache")
    return settings, FocusIndex(mod_root, settings.cache_dir)


def _duplicate_tree(second_x: int = 9) -> str:
    return (
        "focus_tree = {\n"
        "\tfocus = {\n"
        "\t\tid = TST_dup\n"
        "\t\tx = 1\n"
        "\t}\n"
        "\tfocus = {\n"
        "\t\tid = TST_dup\n"
        f"\t\tx = {second_x}\n"
        "\t}\n"
        "}\n"
    )


class _FrozenFocusIndex:
    """Duck-typed FocusIndex whose `resolve` returns a fixed record unchanged."""

    def __init__(self, rec: dict):
        self._rec = rec

    def resolve(self, key: str) -> dict:
        return self._rec


def test_resolve_focus_returns_parsed_details_for_unique_focus(tmp_path):
    settings, index = _focus_mod(
        tmp_path,
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_a\n\t\tx = 4\n\t\ty = 5\n\t}\n}\n",
    )

    result = resolve_focus_tool("TST_a", settings, index)

    assert result["ok"] is True
    assert Path(result["file"]) == Path("common/national_focus/focus_0.txt")
    assert result["kind"] == "focus_tree"
    assert result["line"] == 2
    assert result["parsed"]["x"] == 4
    assert result["parsed"]["y"] == 5


@pytest.mark.parametrize("shared", [False, True])
def test_resolve_focus_anchors_duplicate_to_the_indexed_later_record(tmp_path, shared):
    container = ("shared_focus", "}\n") if shared else ("focus_tree", "}\n")
    text = (
        container[0]
        + " {\n"
        + "\tfocus = {\n\t\tid = TST_dup\n\t\tx = 1\n\t}\n"
        + "\tfocus = {\n\t\tid = TST_dup\n\t\tx = 9\n\t}\n"
        + container[1]
    )
    if shared:
        # shared_focus holds the definitions directly; drop the inner `focus =`.
        text = (
            "shared_focus = {\n"
            "\tid = TST_dup\n"
            "\tx = 1\n"
            "}\n"
            "shared_focus = {\n"
            "\tid = TST_dup\n"
            "\tx = 9\n"
            "}\n"
        )
    settings, index = _focus_mod(tmp_path, text)
    rec = index.resolve("TST_dup")
    assert rec is not None
    # The index keys the last occurrence in file order.
    assert rec["line"] == (5 if shared else 6)

    result = resolve_focus_tool("TST_dup", settings, index)

    assert result["ok"] is True
    assert result["line"] == (5 if shared else 6)
    assert result["parsed"]["x"] == 9


def test_resolve_focus_anchors_duplicate_across_files_to_the_indexed_winner(tmp_path):
    # Canonical relpath order makes focus_1.txt the last-write-wins contributor.
    settings, index = _focus_mod(
        tmp_path,
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_dup\n\t\tx = 1\n\t}\n}\n",
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_dup\n\t\tx = 9\n\t}\n}\n",
    )
    rec = index.resolve("TST_dup")
    assert rec is not None
    assert Path(rec["file"]) == Path("common/national_focus/focus_1.txt")

    result = resolve_focus_tool("TST_dup", settings, index)

    assert result["ok"] is True
    assert Path(result["file"]) == Path("common/national_focus/focus_1.txt")
    assert result["parsed"]["x"] == 9


def test_resolve_focus_walks_tree_shared_and_joint_candidates(tmp_path):
    settings, index = _focus_mod(
        tmp_path,
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_tree\n\t\tx = 1\n\t}\n}\n"
        "shared_focus = {\n\tid = TST_shared\n\tx = 2\n}\n"
        "joint_focus = {\n\tid = TST_joint\n\tx = 3\n}\n",
    )

    tree = resolve_focus_tool("TST_tree", settings, index)
    shared = resolve_focus_tool("TST_shared", settings, index)
    joint = resolve_focus_tool("TST_joint", settings, index)

    assert (tree["kind"], tree["line"]) == ("focus_tree", 2)
    assert (shared["kind"], shared["line"]) == ("shared_focus", 7)
    assert (joint["kind"], joint["line"]) == ("joint_focus", 11)
    assert [tree["parsed"]["x"], shared["parsed"]["x"], joint["parsed"]["x"]] == [1, 2, 3]


def test_resolve_focus_on_cross_kind_duplicate_anchors_indexed_record(tmp_path):
    # A tree focus and a joint_focus sharing an id: the top-level joint_focus is
    # the file-later definition and therefore the index winner.
    settings, index = _focus_mod(
        tmp_path,
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_dup\n\t\tx = 1\n\t}\n}\n"
        "joint_focus = {\n\tid = TST_dup\n\tx = 3\n}\n",
    )
    rec = index.resolve("TST_dup")
    assert rec is not None
    assert rec["kind"] == "joint_focus"

    result = resolve_focus_tool("TST_dup", settings, index)

    assert result["ok"] is True
    assert result["kind"] == "joint_focus"
    assert result["parsed"]["x"] == 3


def test_resolve_focus_stale_indexed_line_returns_clear_error(tmp_path):
    text = "focus_tree = {\n\tfocus = {\n\t\tid = TST_moved\n\t}\n}\n"
    settings, index = _focus_mod(tmp_path, text)
    rec = index.resolve("TST_moved")
    assert rec is not None
    # The focus moved after indexing: simulate the index still pointing at the
    # focus_tree container line, where no definition starts.
    frozen = _FrozenFocusIndex({**rec, "line": 1})

    result = resolve_focus_tool("TST_moved", settings, cast(FocusIndex, frozen))

    assert result["ok"] is False
    assert "stale" in result["error"]
    assert "build-index" in result["error"]
    assert result["line"] == 1
    assert result["kind"] == rec["kind"]


def test_resolve_focus_disappeared_id_returns_clear_error(tmp_path):
    settings, index = _focus_mod(
        tmp_path, "focus_tree = {\n\tfocus = {\n\t\tid = TST_gone\n\t}\n}\n"
    )
    rec = index.resolve("TST_gone")
    assert rec is not None
    # The id no longer exists in the file at all, but the stale index still
    # resolves it: simulate by freezing the record and rewriting the file.
    rewritten = (settings.mod_root / rec["file"]).resolve()
    rewritten.write_text(
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_other\n\t}\n}\n", encoding="utf-8"
    )
    frozen = _FrozenFocusIndex(rec)

    result = resolve_focus_tool("TST_gone", settings, cast(FocusIndex, frozen))

    assert result["ok"] is False
    assert "stale" in result["error"]
    assert "build-index" in result["error"]


def test_resolve_focus_same_line_duplicates_return_clear_ambiguity_error(tmp_path):
    settings, index = _focus_mod(
        tmp_path, "focus_tree = { focus = { id = TST_d } focus = { id = TST_d } }\n"
    )
    rec = index.resolve("TST_d")
    assert rec is not None
    assert rec["line"] == 1

    result = resolve_focus_tool("TST_d", settings, index)

    assert result["ok"] is False
    assert "ambiguous" in result["error"]
    assert "line 1" in result["error"]


def test_resolve_focus_several_candidates_without_indexed_line_is_ambiguous(tmp_path):
    settings, index = _focus_mod(tmp_path, _duplicate_tree())
    rec = index.resolve("TST_dup")
    assert rec is not None
    frozen = _FrozenFocusIndex({**rec, "line": None})

    result = resolve_focus_tool("TST_dup", settings, cast(FocusIndex, frozen))

    assert result["ok"] is False
    assert "ambiguous" in result["error"]
    assert "no line to disambiguate" in result["error"]


def test_resolve_focus_lone_candidate_without_indexed_line_still_resolves(tmp_path):
    settings, index = _focus_mod(
        tmp_path, "focus_tree = {\n\tfocus = {\n\t\tid = TST_a\n\t\tx = 2\n\t}\n}\n"
    )
    rec = index.resolve("TST_a")
    assert rec is not None
    frozen = _FrozenFocusIndex({**rec, "line": None})

    result = resolve_focus_tool("TST_a", settings, cast(FocusIndex, frozen))

    assert result["ok"] is True
    assert result["parsed"]["x"] == 2


def test_index_anchor_error_is_a_keyerror():
    # Resources raise KeyError through the MCP layer; the shared error keeps that
    # contract.
    assert issubclass(IndexAnchorError, KeyError)


@pytest.mark.integration
def test_resolve_focus_and_resource_smoke_on_real_mod(real_mod_root, tmp_path):
    """Smoke: on the real mod every focus id resolves ok and resources slice it."""
    settings = mod_settings(real_mod_root, tmp_path / ".md-mcp-cache")
    index = FocusIndex(real_mod_root, settings.cache_dir)

    ids = [key for key in index.list_keys()[:3]]
    assert ids, "no focus ids indexed at all"

    for focus_id in ids:
        result = resolve_focus_tool(focus_id, settings, index)
        assert result["ok"] is True, (focus_id, result)
        assert result["parsed"] is not None

        resource = focus_resource(focus_id, settings, index)
        assert f"id = {focus_id}" in resource
        assert resource.lstrip().startswith(("focus", "shared_focus", "joint_focus"))
