"""Tests for the index-backed search tools: find_focuses and the definition finders."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from md_mcp.indexes import CountryTagIndex, FocusIndex, ScriptedEffectIndex
from md_mcp.tools import analysis_tools
from md_mcp.util.response import BUDGET_BYTES
from tests.support.mod import in_process_settings


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


@pytest.mark.parametrize(
    ("filters", "ids"),
    [
        ({}, ["TST_root", "TST_branch_a", "TST_branch_b", "TST_shared"]),
        ({"tag": "tst"}, ["TST_root", "TST_branch_a", "TST_branch_b", "TST_shared"]),
        ({"tag": "TS"}, []),
        ({"kind": "shared_focus"}, ["TST_shared"]),
        ({"has_prereq": "TST_root"}, ["TST_branch_a", "TST_branch_b"]),
        ({"mutex_with": "TST_branch_a"}, ["TST_branch_b"]),
        ({"has_prereq": "TST_root", "mutex_with": "TST_branch_b"}, ["TST_branch_a"]),
        # No candidate survives the tag filter, so no file is reparsed.
        ({"tag": "XXX", "has_prereq": "TST_root"}, []),
    ],
)
def test_find_focuses_filters(fake_mod_root, cache_dir, filters, ids):
    out = analysis_tools.find_focuses_tool(
        in_process_settings(fake_mod_root, cache_dir),
        FocusIndex(fake_mod_root, cache_dir),
        **filters,
    )

    assert out["ok"] is True
    assert out["partial"] is False
    assert [match["id"] for match in out["matches"]] == ids


def test_find_focuses_and_find_indexed_report_a_limit_they_cannot_read(fake_mod_root, cache_dir):
    index = FocusIndex(fake_mod_root, cache_dir)

    focuses = analysis_tools.find_focuses_tool(
        in_process_settings(fake_mod_root, cache_dir), index, limit="many"
    )
    indexed = analysis_tools.find_indexed_tool(index, limit="many")

    assert focuses == {"ok": False, "error": "limit must be an integer, got 'many'"}
    assert indexed == focuses


def test_find_indexed_filters_by_query_and_kind_and_pages(fake_mod_root, cache_dir):
    tags = CountryTagIndex(fake_mod_root, cache_dir, include_vanilla=False)
    effects = ScriptedEffectIndex(fake_mod_root, cache_dir, include_vanilla=False)

    assert analysis_tools.find_indexed_tool(tags, query="tst")["matches"] == [
        {
            "id": "TST",
            "kind": "country_tag",
            "file": str(Path("common/country_tags/test_tags.txt")),
            "line": 1,
            "country_file": "countries/Testland.txt",
        }
    ]
    assert analysis_tools.find_indexed_tool(tags, "trait", query="TST")["total"] == 0
    assert analysis_tools.find_indexed_tool(tags, query="absent")["total"] == 0
    page = analysis_tools.find_indexed_tool(effects, query="test", limit=1)
    assert (page["total"], page["returned"], page["truncated"]) == (1, 1, False)


def test_find_indexed_budget_drop_reconciles_pagination_metadata():
    class LargeHistoryIndex:
        def list_files(self):
            return ["history/units/test.txt"]

        def records_for_file(self, _):
            return [
                {
                    "id": f"template-{index}-" + ("x" * 400),
                    "kind": "division_template",
                    "file": "history/units/test.txt",
                    "line": index,
                }
                for index in range(854)
            ]

    out = analysis_tools.find_indexed_tool(LargeHistoryIndex(), "division_template", limit=10_000)

    assert out["total"] == 854
    assert out["returned"] == 0
    assert out["truncated"] is True
    assert out["matches_dropped"] == 854
    assert "matches" not in out


def test_find_focuses_paginates_with_offset(fake_mod_root, cache_dir):
    out = analysis_tools.find_focuses_tool(
        in_process_settings(fake_mod_root, cache_dir),
        FocusIndex(fake_mod_root, cache_dir),
        has_prereq="TST_root",
        limit="1",
        offset=1.9,
    )

    assert out["total"] == 2
    assert out["returned"] == 1
    assert out["count"] == 1
    assert out["truncated"] is False
    assert [match["id"] for match in out["matches"]] == ["TST_branch_b"]


def test_find_focuses_budget_guard_drops_oversized_matches(fake_mod_root, cache_dir, monkeypatch):
    records = [
        {"id": f"TST_{i}", "file": "common/focuses.txt", "line": i, "kind": "focus_tree"}
        for i in range(2_000)
    ]
    monkeypatch.setattr(
        analysis_tools,
        "_all_records",
        lambda _: ((record["id"], record) for record in records),
    )

    out = analysis_tools.find_focuses_tool(
        in_process_settings(fake_mod_root, cache_dir),
        FocusIndex(fake_mod_root, cache_dir),
        limit=2_000,
    )

    assert out["total"] == 2_000
    assert out["returned"] == 2_000
    assert out["size_truncated"] is True
    assert "matches" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_find_focuses_reports_index_parse_failures(fake_mod_root, cache_dir):
    broken = fake_mod_root / "common" / "national_focus" / "broken.txt"
    broken.write_text("focus_tree = { focus = { id = TST_broken x = {{{", encoding="utf-8")

    out = analysis_tools.find_focuses_tool(
        in_process_settings(fake_mod_root, cache_dir),
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
        in_process_settings(fake_mod_root, cache_dir), index, has_prereq="TST_root"
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


@pytest.mark.parametrize(
    ("filters", "line", "kind"),
    [
        ({"has_prereq": "DUP_first"}, 2, "focus_tree"),
        ({"mutex_with": "DUP_first_mutex"}, 2, "focus_tree"),
        ({"has_prereq": "DUP_second", "mutex_with": "DUP_second_mutex"}, 7, "focus_tree"),
        ({"has_prereq": "DUP_shared"}, 13, "shared_focus"),
        ({"has_prereq": "DUP_shared", "kind": "focus_tree"}, None, None),
    ],
)
def test_find_focuses_filters_duplicate_ids_by_location(
    fake_mod_root, cache_dir, filters, line, kind
):
    relpath = "common/national_focus/duplicates.txt"
    (fake_mod_root / relpath).write_text(
        "focus_tree = {\n"
        "    focus = {\n"
        "        id = DUP_same\n"
        "        prerequisite = { focus = DUP_first }\n"
        "        mutually_exclusive = { focus = DUP_first_mutex }\n"
        "    }\n"
        "    focus = {\n"
        "        id = DUP_same\n"
        "        prerequisite = { focus = DUP_second }\n"
        "        mutually_exclusive = { focus = DUP_second_mutex }\n"
        "    }\n"
        "}\n"
        "shared_focus = {\n"
        "    id = DUP_same\n"
        "    prerequisite = { focus = DUP_shared }\n"
        "}\n",
        encoding="utf-8",
    )

    out = analysis_tools.find_focuses_tool(
        in_process_settings(fake_mod_root, cache_dir),
        FocusIndex(fake_mod_root, cache_dir),
        tag="DUP",
        **filters,
    )

    assert out["partial"] is False
    assert out["matches"] == (
        [{"id": "DUP_same", "file": relpath, "line": line, "kind": kind}] if line else []
    )
    assert out["total"] == len(out["matches"])


def test_find_focuses_reports_same_line_duplicate_ambiguity(fake_mod_root, cache_dir):
    relpath = "common/national_focus/duplicates.txt"
    (fake_mod_root / relpath).write_text(
        "focus_tree = { focus = { id = DUP_same prerequisite = { focus = DUP_root } } "
        "focus = { id = DUP_same prerequisite = { focus = DUP_root } } }\n",
        encoding="utf-8",
    )

    out = analysis_tools.find_focuses_tool(
        in_process_settings(fake_mod_root, cache_dir),
        FocusIndex(fake_mod_root, cache_dir),
        tag="DUP",
        has_prereq="DUP_root",
    )

    assert out["matches"] == []
    assert out["partial"] is True
    assert out["skipped_records"] == 2
    assert out["skipped_files"] == 0
    assert out["partial_errors_total"] == 2
    assert all("ambiguous" in error["error"] for error in out["partial_errors"])


def test_find_focuses_reports_moved_indexed_line(fake_mod_root, cache_dir, monkeypatch):
    source = fake_mod_root / "common/national_focus/stale.txt"
    text = _focus_text("DUP_moved")
    source.write_text(text, encoding="utf-8")
    index = FocusIndex(fake_mod_root, cache_dir)
    index.ensure_fresh()
    source.write_text("\n" + text, encoding="utf-8")
    monkeypatch.setattr(index._stale_check, "should_check", lambda: False)

    out = analysis_tools.find_focuses_tool(
        in_process_settings(fake_mod_root, cache_dir), index, tag="DUP", has_prereq="TST_root"
    )

    assert out["matches"] == []
    assert out["partial"] is True
    assert out["skipped_records"] == 1
    assert out["partial_errors_total"] == 1
    assert "stale" in out["partial_errors"][0]["error"]


def test_find_focuses_bounds_partial_errors(fake_mod_root, cache_dir, monkeypatch):
    focus_dir = fake_mod_root / "common" / "national_focus"
    for name in ("broken_a.txt", "broken_b.txt"):
        (focus_dir / name).write_text(
            "focus_tree = { focus = { id = TST_broken x = {{{", encoding="utf-8"
        )
    monkeypatch.setattr(analysis_tools, "_MAX_PARTIAL_ERRORS", 1)

    out = analysis_tools.find_focuses_tool(
        in_process_settings(fake_mod_root, cache_dir),
        FocusIndex(fake_mod_root, cache_dir),
        has_prereq="TST_root",
    )

    assert out["partial_errors_total"] == 2
    assert len(out["partial_errors"]) == 1
    assert out["partial_errors_truncated"] is True
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
