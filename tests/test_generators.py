"""Generator tests — every emitted block must round-trip through the parser."""

from __future__ import annotations

import pytest

from md_mcp.generators import (
    generate_decision,
    generate_event,
    generate_focus,
    generate_gfx_entry,
    generate_idea,
    generate_loc_stub,
)
from md_mcp.paradox import parse_string
from md_mcp.paradox.schema import (
    extract_decision_records,
    extract_event_records,
    extract_focus_records,
    extract_idea_records,
    extract_sprite_records,
)


def test_focus_round_trips_and_extracts_correctly():
    r = generate_focus(
        id="TST_root",
        tag="TST",
        x=0,
        y=0,
        cost=10,
        completion_reward="add_political_power = 50",
    )
    # Wrap in focus_tree so the schema extractor sees it the way it would in-game.
    wrapped = "focus_tree = { id = test\n" + r["txt"] + "\n}"
    root = parse_string(wrapped)
    records = extract_focus_records(root, source=wrapped)
    assert any(rec["id"] == "TST_root" for rec in records)
    keys = {entry["key"] for entry in r["loc_yml_keys"]}
    assert keys == {"TST_root", "TST_root_desc"}


def test_focus_emits_prereq_and_mutex_correctly():
    r = generate_focus(
        id="TST_branch",
        tag="TST",
        x=1,
        y=1,
        relative_position_id="TST_root",
        prerequisites=[["TST_root"]],
        mutually_exclusive=["TST_other"],
    )
    wrapped = "focus_tree = { id = test\n" + r["txt"] + "\n}"
    root = parse_string(wrapped)
    [rec] = extract_focus_records(root)
    assert rec["prerequisites"] == [["TST_root"]]
    assert rec["mutually_exclusive"] == ["TST_other"]
    assert rec["relative_position_id"] == "TST_root"


def test_focus_includes_log_line():
    r = generate_focus(id="TST_x", tag="TST", x=0, y=0)
    assert 'log = "[GetDateText]: [Root.GetName]: Focus TST_x"' in r["txt"]


def test_event_round_trips_with_options():
    r = generate_event(
        namespace="Test",
        number=1,
        options=[
            {"label": "Yes", "effects": "add_political_power = 25"},
            {"label": "No"},
        ],
    )
    root = parse_string(r["txt"])
    [rec] = extract_event_records(root)
    assert rec["id"] == "Test.1"
    assert rec["kind"] == "country_event"

    keys = {entry["key"] for entry in r["loc_yml_keys"]}
    assert keys == {"Test.1.t", "Test.1.d", "Test.1.a", "Test.1.b"}
    assert r["namespace_directive"] == "add_namespace = Test"


def test_event_invalid_kind_rejected():
    with pytest.raises(ValueError):
        generate_event(namespace="X", number=1, kind="bogus_event")


def test_event_too_many_options_rejected():
    with pytest.raises(ValueError):
        generate_event(namespace="X", number=1, options=[{}] * 27)


def test_decision_round_trips():
    r = generate_decision(id="TST_dec", tag="TST", cost=50, complete_effect="add_stability = 0.05")
    # Wrap in a category since the extractor requires that nesting.
    wrapped = "TST_cat = {\n" + r["txt"] + "\n}"
    root = parse_string(wrapped)
    records = extract_decision_records(root)
    assert [rec["id"] for rec in records] == ["TST_dec"]
    assert records[0]["category"] == "TST_cat"
    # `allowed = { original_tag = TAG }` is auto-built from the tag arg.
    assert "original_tag = TST" in r["txt"]


def test_idea_round_trips():
    r = generate_idea(id="TST_idea", tag="TST", modifier="stability_factor = 0.05")
    wrapped = "ideas = { country = {\n" + r["txt"] + "\n} }"
    root = parse_string(wrapped)
    records = extract_idea_records(root)
    assert [rec["id"] for rec in records] == ["TST_idea"]
    assert "original_tag = TST" in r["txt"]


def test_gfx_round_trips():
    r = generate_gfx_entry(name="GFX_test", texturefile="gfx/test.dds", frames=4)
    wrapped = "spriteTypes = {\n" + r["txt"] + "\n}"
    root = parse_string(wrapped)
    [rec] = extract_sprite_records(root)
    assert rec["name"] == "GFX_test"
    assert rec["texturefile"] == "gfx/test.dds"


def test_loc_stub_bom_and_escaping():
    r = generate_loc_stub(
        [
            {"key": "K1", "value": "Simple"},
            {"key": "K2", "value": 'has "quotes" and a \\ backslash'},
        ],
        bom_prefix=True,
    )
    assert r["txt"].startswith("﻿")
    assert "l_english:" in r["txt"]
    assert r"\"" in r["txt"]
    assert r["bytes_to_write"].startswith(b"\xef\xbb\xbf")


def test_loc_stub_append_mode_skips_header():
    r = generate_loc_stub([{"key": "K", "value": "V"}], include_header=False)
    assert "l_english:" not in r["txt"]
    assert ' K: "V"' in r["txt"]
