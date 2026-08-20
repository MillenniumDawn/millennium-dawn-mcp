"""Schema-layer extractor tests."""

from __future__ import annotations

from md_mcp.paradox import parse_string
from md_mcp.paradox.schema import (
    extract_event_records,
    extract_focus_ids,
    extract_focus_records,
    extract_sprite_records,
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


EVENTS_SAMPLE = """add_namespace = TST

country_event = {
\tid = TST.1
\ttitle = TST.1.t
}

news_event = {
\tid = TST.2
\ttitle = TST.2.t
}

add_namespace = TST_late

state_event = {
\tid = TST.3
\ttitle = TST.3.t
}

country_event = {
\ttitle = TST.4.t
}

add_namespace = "TST_quoted"
"""


def test_extract_event_records_pins_kind_namespace_and_line():
    root = parse_string(EVENTS_SAMPLE)
    records = extract_event_records(root, source=EVENTS_SAMPLE)
    by_id = {r["id"]: r for r in records}

    assert by_id["TST.1"]["kind"] == "country_event"
    assert by_id["TST.2"]["kind"] == "news_event"
    assert by_id["TST.3"]["kind"] == "state_event"
    assert by_id["TST.1"]["namespace"] == "TST"
    assert all(r["line"] is not None and r["line"] > 0 for r in records)


def test_extract_event_records_skips_event_without_id():
    root = parse_string(EVENTS_SAMPLE)
    records = extract_event_records(root, source=EVENTS_SAMPLE)
    # TST.4's block has no `id` key, so it must not appear as a record at all.
    assert "TST.4" not in {r["id"] for r in records}
    assert len(records) == 3


def test_extract_event_records_file_namespaces_include_later_declarations():
    root = parse_string(EVENTS_SAMPLE)
    records = extract_event_records(root, source=EVENTS_SAMPLE)
    by_id = {r["id"]: r for r in records}

    # add_namespace = TST_late comes after TST.1/TST.2 in the file but must still
    # appear in every record's file_namespaces, including events declared earlier.
    # TST_quoted is declared as a quoted string rather than a bare symbol.
    assert by_id["TST.1"]["file_namespaces"] == ["TST", "TST_late", "TST_quoted"]
    assert by_id["TST.3"]["file_namespaces"] == ["TST", "TST_late", "TST_quoted"]


SPRITES_SAMPLE = """bitmapfonts = {
\tname = "not_a_sprite_block"
}

spriteTypes = {
\tspriteType = {
\t\tname = "GFX_a"
\t\ttexturefile = "gfx/a.dds"
\t}
\tcorneredTileSpriteType = {
\t\tname = "GFX_b"
\t\ttexturefile = "gfx/b.dds"
\t}
\tsomeOtherBlock = {
\t\tname = "GFX_c"
\t\ttexturefile = "gfx/c.dds"
\t}
\tspriteType = {
\t\ttexturefile = "gfx/no_name.dds"
\t}
\tspriteType = {
\t\tname = 5
\t\ttexturefile = "gfx/numeric_name.dds"
\t}
}

SpriteTypes_extra = {
\tspriteType = {
\t\tname = "GFX_d"
\t\ttexturefile = "gfx/d.dds"
\t}
}
"""


def test_extract_sprite_records_pins_kind_parent_and_line():
    root = parse_string(SPRITES_SAMPLE)
    records = extract_sprite_records(root, source=SPRITES_SAMPLE)
    by_name = {r["name"]: r for r in records}

    assert by_name["GFX_a"]["kind"] == "spriteType"
    assert by_name["GFX_a"]["parent"] == "spriteTypes"
    assert by_name["GFX_b"]["kind"] == "corneredTileSpriteType"
    assert all(r["line"] is not None and r["line"] > 0 for r in records)


def test_extract_sprite_records_skips_non_sprite_child():
    root = parse_string(SPRITES_SAMPLE)
    records = extract_sprite_records(root, source=SPRITES_SAMPLE)
    # someOtherBlock isn't a recognised sprite kind, so GFX_c must not appear.
    assert "GFX_c" not in {r["name"] for r in records}


def test_extract_sprite_records_skips_non_spritetypes_top_level_block():
    root = parse_string(SPRITES_SAMPLE)
    records = extract_sprite_records(root, source=SPRITES_SAMPLE)
    # bitmapfonts isn't a spriteTypes-prefixed block, so it must be skipped entirely.
    assert "not_a_sprite_block" not in {r["name"] for r in records}


def test_extract_sprite_records_skips_sprite_without_name():
    root = parse_string(SPRITES_SAMPLE)
    records = extract_sprite_records(root, source=SPRITES_SAMPLE)
    assert "gfx/no_name.dds" not in {r["texturefile"] for r in records}


def test_extract_sprite_records_skips_sprite_with_non_string_name():
    root = parse_string(SPRITES_SAMPLE)
    records = extract_sprite_records(root, source=SPRITES_SAMPLE)
    assert "gfx/numeric_name.dds" not in {r["texturefile"] for r in records}


def test_extract_sprite_records_matches_spritetypes_prefix_case_insensitively():
    root = parse_string(SPRITES_SAMPLE)
    records = extract_sprite_records(root, source=SPRITES_SAMPLE)
    by_name = {r["name"]: r for r in records}

    assert by_name["GFX_d"]["parent"] == "SpriteTypes_extra"
