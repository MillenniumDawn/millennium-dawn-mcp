"""Schema-layer extractor tests."""

from __future__ import annotations

from typing import Any, cast

import pytest

from md_mcp.paradox import parse_string
from md_mcp.paradox.nodes import Node
from md_mcp.paradox.schema import (
    extract_decision_records,
    extract_event_records,
    extract_focus_records,
    extract_idea_records,
    extract_sprite_records,
    find_idea_nodes,
    focus_icon_choices,
    focus_mutually_exclusive,
    focus_prerequisites,
    is_focus_file_content,
    to_json_with_lines,
)
from md_mcp.util.line_numbers import line_starts, pos_to_line

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

    # Line numbers are hand-counted from SAMPLE: A's `focus = {` is line 4,
    # B's is line 10, S's `shared_focus = {` is line 20.
    assert by_id["A"]["line"] == 4
    assert by_id["B"]["line"] == 10
    assert by_id["S"]["line"] == 20


RELATIONS_TEMPLATE = """focus_tree = {{
    focus = {{
        id = "A"
        prerequisite = {{ focus = {b} focus = {c} }}
        prerequisite = {{ focus = {d} }}
        mutually_exclusive = {{ focus = {e} focus = {f} }}
    }}
}}
"""


@pytest.mark.parametrize(
    "refs",
    [
        pytest.param("B C D E F", id="unquoted"),
        pytest.param('"B" "C" "D" "E" "F"', id="quoted"),
        pytest.param('"B" C D "E" F', id="mixed"),
    ],
)
def test_extract_focus_records_relations_ignore_quoting(refs):
    b, c, d, e, f = refs.split()
    root = parse_string(RELATIONS_TEMPLATE.format(b=b, c=c, d=d, e=e, f=f))
    (rec,) = extract_focus_records(root)

    # Each prerequisite block is one OR group; the groups are ANDed.
    assert rec["prerequisites"] == [["B", "C"], ["D"]]
    assert rec["mutually_exclusive"] == ["E", "F"]


def test_extract_focus_records_relations_drop_non_text_values():
    source = """focus_tree = {
    focus = {
        id = A
        prerequisite = { focus = 5 }
        prerequisite = { focus = { } focus = "B" }
        mutually_exclusive = { focus = 5 focus = { x = 1 } focus = "C" }
    }
}
"""
    (rec,) = extract_focus_records(parse_string(source))

    assert rec["prerequisites"] == [["B"]]
    assert rec["mutually_exclusive"] == ["C"]


def test_extract_focus_records_skips_a_focus_without_an_id():
    assert extract_focus_records(parse_string("focus_tree = { focus = { x = 1 } }")) == []


def test_extract_focus_records_reads_yes_and_no_as_bools():
    source = "shared_focus = { id = A x = yes y = no }"
    (rec,) = extract_focus_records(parse_string(source))

    assert (rec["x"], rec["y"]) == (True, False)


def test_to_json_with_lines_emits_line_numbers():
    root = parse_string(SAMPLE)
    j = to_json_with_lines(root, SAMPLE)
    # First top-level child is focus_tree.
    focus_tree = j["value"]["children"][0]
    assert focus_tree["name"] == "focus_tree"
    # SAMPLE's `focus_tree = {` is line 2 (line 1 is the leading blank line).
    assert focus_tree["line"] == 2


def test_to_json_with_lines_numbers_every_sibling():
    src = "a = 1\nb = 2\nc = { d = 3 }"
    children = to_json_with_lines(parse_string(src), src)["value"]["children"]

    assert [(child["name"], child["line"]) for child in children] == [("a", 1), ("b", 2), ("c", 3)]


def test_to_json_with_lines_rejects_a_value_it_cannot_represent():
    node = Node(name="a", value=cast(Any, object()))

    with pytest.raises(TypeError, match="Unrepresentable value of type object"):
        to_json_with_lines(node, "")


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
    # Line numbers are hand-counted from EVENTS_SAMPLE: each `*_event = {` line.
    assert by_id["TST.1"]["line"] == 3
    assert by_id["TST.2"]["line"] == 8
    assert by_id["TST.3"]["line"] == 15
    # The fourth block has no `id` key, so it is not a record at all.
    assert [r["id"] for r in records] == ["TST.1", "TST.2", "TST.3"]


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
    # The spriteTypes prefix matches without regard to case.
    assert by_name["GFX_d"]["parent"] == "SpriteTypes_extra"
    # Line numbers are hand-counted from SPRITES_SAMPLE: each `spriteType = {` line.
    assert by_name["GFX_a"]["line"] == 6
    assert by_name["GFX_b"]["line"] == 10
    assert by_name["GFX_d"]["line"] == 28


def test_extract_sprite_records_keeps_only_named_sprites_of_a_known_kind():
    records = extract_sprite_records(parse_string(SPRITES_SAMPLE), source=SPRITES_SAMPLE)

    # Skipped: the bitmapfonts block (not spriteTypes), GFX_c (someOtherBlock is
    # not a sprite kind), the sprite with no name, and the one named with a number.
    assert [(r["name"], r["texturefile"]) for r in records] == [
        ("GFX_a", "gfx/a.dds"),
        ("GFX_b", "gfx/b.dds"),
        ("GFX_d", "gfx/d.dds"),
    ]


DECISIONS_SAMPLE = """some_category = {
\ticon = GFX_decision_category

\tTST_first_decision = {
\t\tcost = 10
\t}

\tTST_second_decision = {
\t\tcost = 20
\t}
}
"""


def test_extract_decision_records_pins_category_and_line():
    root = parse_string(DECISIONS_SAMPLE)
    records = extract_decision_records(root, source=DECISIONS_SAMPLE)
    by_id = {r["id"]: r for r in records}

    assert by_id["TST_first_decision"]["category"] == "some_category"
    assert by_id["TST_second_decision"]["category"] == "some_category"
    # Line numbers are hand-counted from DECISIONS_SAMPLE: each decision's `= {` line.
    assert by_id["TST_first_decision"]["line"] == 4
    assert by_id["TST_second_decision"]["line"] == 8


def test_extract_decision_records_skips_scalars_at_both_levels():
    source = "version = 1\nsome_category = { note = 5 TST_decision = { cost = 1 } }"
    records = extract_decision_records(parse_string(source))

    assert [r["id"] for r in records] == ["TST_decision"]


IDEAS_SAMPLE = """ideas = {
\tcountry = {
\t\tTST_first_idea = {
\t\t\tpicture = generic_foo
\t\t}

\t\tTST_second_idea = {
\t\t\tpicture = generic_bar
\t\t}
\t}
}
"""


def test_extract_idea_records_pins_category_slot_and_line():
    root = parse_string(IDEAS_SAMPLE)
    records = extract_idea_records(root, source=IDEAS_SAMPLE)
    by_id = {r["id"]: r for r in records}

    assert by_id["TST_first_idea"]["category"] == "country"
    assert by_id["TST_first_idea"]["slot"] is None
    # Line numbers are hand-counted from IDEAS_SAMPLE: each idea's `= {` line.
    assert by_id["TST_first_idea"]["line"] == 3
    assert by_id["TST_second_idea"]["line"] == 7


def _ideas(script: str) -> dict[str, dict]:
    return {r["id"]: r for r in extract_idea_records(parse_string(script))}


def test_extract_idea_records_without_an_ideas_block_is_empty():
    assert _ideas("country = { TST_idea = { picture = x } }") == {}


def test_extract_idea_records_skips_scalars_at_both_levels():
    recs = _ideas(
        "ideas = { note = 1 country = { law = yes cost = 5 TST_idea = { picture = x } } }"
    )

    assert list(recs) == ["TST_idea"]


def test_extract_idea_records_reads_ideas_inside_a_slot_wrapper():
    recs = _ideas(
        """ideas = {
            country = {
                TST_slot = {
                    law = yes
                    note = 1
                    TST_inner = { picture = x }
                }
            }
        }"""
    )

    # The wrapper is the slot, not an idea, and its scalars are not ideas either.
    assert recs == {
        "TST_inner": {"id": "TST_inner", "category": "country", "slot": "TST_slot", "line": None}
    }


# _looks_like_slot_wrapper once misread the next three shapes (issue #57).


def test_empty_idea_block_is_indexed_as_idea():
    assert "TST_empty" in _ideas("ideas = { country = { TST_empty = {} } }")


def test_idea_with_only_unknown_property_keys_is_indexed():
    recs = _ideas(
        """ideas = {
            country = {
                TST_weird = {
                    some_future_key = yes
                    another_unknown = 3
                }
            }
        }"""
    )
    assert "TST_weird" in recs
    # Its scalar children must not be promoted to ideas of their own.
    assert "some_future_key" not in recs
    assert "another_unknown" not in recs


def test_slot_keyword_beside_an_idea_is_not_an_idea():
    recs = _ideas(
        """ideas = {
            tank_manufacturer = {
                designer = yes
                TST_acme = {
                    cost = 150
                    picture = company_default
                }
            }
        }"""
    )
    assert "TST_acme" in recs
    assert recs["TST_acme"]["category"] == "tank_manufacturer"
    assert "designer" not in recs


@pytest.mark.parametrize("prop", ["allowed_civil_war", "on_add", "on_remove"])
def test_idea_holding_only_lifecycle_property_is_indexed(prop: str):
    recs = _ideas(
        f"ideas = {{ hidden_ideas = {{ TST_idea = {{ {prop} = {{ always = yes }} }} }} }}"
    )

    assert list(recs) == ["TST_idea"]
    assert recs["TST_idea"] == {
        "id": "TST_idea",
        "category": "hidden_ideas",
        "slot": None,
        "line": None,
    }


def test_lifecycle_only_idea_sits_beside_empty_ideas_and_slots():
    recs = _ideas(
        """ideas = {
            hidden_ideas = {
                TST_monitor = { allowed_civil_war = { always = yes } }
                TST_empty = {}
            }
            country = {
                TST_slot_wrapper = {
                    law = yes
                    TST_inner = { picture = x }
                }
            }
        }"""
    )

    assert recs == {
        "TST_monitor": {
            "id": "TST_monitor",
            "category": "hidden_ideas",
            "slot": None,
            "line": None,
        },
        "TST_empty": {
            "id": "TST_empty",
            "category": "hidden_ideas",
            "slot": None,
            "line": None,
        },
        "TST_inner": {
            "id": "TST_inner",
            "category": "country",
            "slot": "TST_slot_wrapper",
            "line": None,
        },
    }
    # Neither the lifecycle property nor the slot keyword became idea IDs.
    assert "allowed_civil_war" not in recs
    assert "law" not in recs


def test_find_idea_nodes_matches_every_extracted_idea_record():
    script = """ideas = {
\thidden_ideas = {
\t\tTST_monitor = {
\t\t\ton_add = { add_stability = 0.05 }
\t\t}
\t}
\tcountry = {
\t\tlaw = yes
\t\tTST_plain = {
\t\t\tpicture = generic_foo
\t\t}
\t}
}
"""
    root = parse_string(script)
    records = extract_idea_records(root, source=script)
    starts = line_starts(script)

    assert {r["id"] for r in records} == {"TST_monitor", "TST_plain"}
    for record in records:
        nodes = find_idea_nodes(root, record["id"])
        assert len(nodes) == 1
        assert nodes[0].name == record["id"]
        assert nodes[0].name_token is not None
        assert pos_to_line(nodes[0].name_token.start, starts) == record["line"]


def test_extract_focus_records_reads_every_mutually_exclusive_block():
    root = parse_string(
        "focus_tree = { focus = { id = A mutually_exclusive = { focus = B } "
        "mutually_exclusive = { focus = C } } }"
    )

    (record,) = extract_focus_records(root)

    assert record["mutually_exclusive"] == ["B", "C"]


def test_focus_links_keep_or_groups_and_drop_empty_blocks():
    focus = parse_string(
        "focus = { id = A prerequisite = { focus = P1 focus = P2 } prerequisite = { focus = P3 } "
        "prerequisite = { } mutually_exclusive = { focus = B } mutually_exclusive = { } }"
    ).children()[0]

    assert focus_prerequisites(focus) == [["P1", "P2"], ["P3"]]
    assert focus_mutually_exclusive(focus) == ["B"]


def test_focus_icon_choices_cover_the_three_icon_forms():
    focus = parse_string(
        "focus = { icon = GFX_plain "
        "icon = { trigger = { tag = AAA } value = GFX_old } "
        "icon = { GFX_new = { tag = BBB } GFX_fallback = yes } }"
    ).children()[0]

    choices = focus_icon_choices(focus)

    assert [name for name, _gate in choices] == ["GFX_plain", "GFX_old", "GFX_new", "GFX_fallback"]
    assert choices[0][1] is None and choices[3][1] is None
    old_gate, new_gate = choices[1][1], choices[2][1]
    assert old_gate is not None and old_gate.name == "trigger"
    assert new_gate is not None and new_gate.name == "GFX_new"
