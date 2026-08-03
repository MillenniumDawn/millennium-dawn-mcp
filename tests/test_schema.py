"""Schema-layer extractor tests."""

from __future__ import annotations

from md_mcp.paradox import parse_string
from md_mcp.paradox.schema import (
    extract_focus_ids,
    extract_focus_records,
    is_focus_file_content,
    to_json_with_lines,
)

SAMPLE = """
focus_tree = {
    id = sample_tree
    focus = {
        id = A
        x = 0
        y = 0
        cost = 10
    }
    focus = {
        id = B
        x = 1
        y = 1
        cost = 5
        prerequisite = { focus = A }
        mutually_exclusive = { focus = C }
    }
}

shared_focus = {
    id = S
    x = 0
    y = 0
    cost = 1
}
"""


def test_is_focus_file_content_detects_all_three_kinds():
    assert is_focus_file_content("focus_tree = {}")
    assert is_focus_file_content("shared_focus = {}")
    assert is_focus_file_content("joint_focus = {}")
    assert not is_focus_file_content("idea = {}")


def test_extract_focus_ids_all_kinds():
    root = parse_string(SAMPLE)
    assert extract_focus_ids(root) == ["A", "B", "S"]


def test_extract_focus_records_includes_metadata():
    root = parse_string(SAMPLE)
    records = extract_focus_records(root, source=SAMPLE)
    by_id = {r["id"]: r for r in records}

    assert by_id["A"]["kind"] == "focus_tree"
    assert by_id["A"]["x"] == 0
    assert by_id["A"]["cost"] == 10

    assert by_id["B"]["prerequisites"] == [["A"]]
    assert by_id["B"]["mutually_exclusive"] == ["C"]

    assert by_id["S"]["kind"] == "shared_focus"

    # All records should have line numbers when source is supplied.
    assert all(r["line"] is not None and r["line"] > 0 for r in records)


def test_to_json_with_lines_emits_line_numbers():
    root = parse_string(SAMPLE)
    j = to_json_with_lines(root, SAMPLE)
    # First top-level child is focus_tree.
    focus_tree = j["value"]["children"][0]
    assert focus_tree["name"] == "focus_tree"
    assert focus_tree["line"] is not None and focus_tree["line"] > 0
