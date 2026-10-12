"""Resource handler tests: each handler returns the exact source of the indexed definition."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, NamedTuple

import pytest

from md_mcp.config import Settings
from md_mcp.indexes import (
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
)
from md_mcp.paradox.nodes import Node, SymbolNode
from md_mcp.resources import (
    _extract_focus_block,
    _slice_node,
    decision_resource,
    event_resource,
    focus_resource,
    idea_resource,
    loc_resource,
    sprite_resource,
)
from tests.support.mod import mod_settings


class _Kind(NamedTuple):
    path: str
    index: Callable[..., Any]
    resource: Callable[..., str]


KINDS = {
    "decision": _Kind("common/decisions/test.txt", DecisionIndex, decision_resource),
    "idea": _Kind("common/ideas/test.txt", IdeaIndex, idea_resource),
    "event": _Kind("events/test.txt", EventIndex, event_resource),
    "sprite": _Kind("interface/test.gfx", GfxIndex, sprite_resource),
    "focus": _Kind("common/national_focus/test.txt", FocusIndex, focus_resource),
}

# Leave the record's line as the index built it.
INDEXED: Any = object()


class _FakeIndex:
    """Stand-in for an index that resolves every key to one fixed record."""

    def __init__(self, rec: dict):
        self._rec = rec

    def resolve(self, key: str) -> dict:
        return self._rec


def _index(tmp_path: Path, kind: str, text: str) -> tuple[Path, Settings, Any]:
    """Write `text` as the mod's only file of `kind` and index it."""
    mod_root = tmp_path / "Mod"
    path = mod_root / KINDS[kind].path
    path.parent.mkdir(parents=True)
    # Bytes, not write_text: exact-source assertions need LF on every platform.
    path.write_bytes(text.encode("utf-8"))
    settings = mod_settings(mod_root, tmp_path / ".cache")
    return mod_root, settings, KINDS[kind].index(mod_root, settings.cache_dir)


def _read(tmp_path: Path, kind: str, text: str, ident: str, line: Any = INDEXED) -> str:
    """Read `ident` through the resource, optionally with the record's line replaced.

    `line=None` is an index that recorded no line. `line=1` is a line no
    definition in these fixtures starts on.
    """
    _, settings, index = _index(tmp_path, kind, text)
    if line is not INDEXED:
        rec = index.resolve(ident)
        assert rec is not None
        index = _FakeIndex({**rec, "line": line})
    return KINDS[kind].resource(ident, settings, index)


DECISIONS_WITH_IMPOSTOR = """TST_category = {
\ticon = generic_decision_category

\tTST_impostor_home = {
\t\tallowed = { tag = TST }
\t\tcomplete_effect = {
\t\t\tTST_real = {
\t\t\t\tsome_effect = yes
\t\t\t}
\t\t\tadd_political_power = 50
\t\t}
\t}

\tTST_real = {
\t\tallowed = { tag = TST }
\t\tcost = 25
\t}
}
"""

IDEAS_WITH_IMPOSTOR = """ideas = {
\tcountry = {
\t\tTST_impostor_home = {
\t\t\tmodifier = {
\t\t\t\tTST_real = {
\t\t\t\t\tstability_factor = 0.05
\t\t\t\t}
\t\t\t}
\t\t\tallowed = { original_tag = TST }
\t\t}

\t\tTST_real = {
\t\t\tpicture = generic_idea
\t\t\tmodifier = {
\t\t\t\twar_support_factor = 0.05
\t\t\t}
\t\t}
\t}
}
"""

DECISIONS_WITH_DUPES = """TST_category = {
\tTST_dup = {
\t\tcost = 10
\t}

\tTST_dup = {
\t\tcost = 20
\t}
}
"""

DECISIONS_WITH_COMMENT = (
    "TST_category = {\n" "\tTST_commented = {\n" "\t\tcost = 30 # important note\n" "\t}\n" "}\n"
)

DECISION_AFTER_SAME_LINE_OPENER = (
    "TST_category = { TST_dec = { # opening note\n"
    "\tcost = 1 # trailing note\n"
    "\n"
    "\t# interior note\n"
    "} }\n"
)

EVENTS_WITH_IMPOSTOR = """add_namespace = TST

country_event = {
\tid = TST.1
\ttitle = TST.1.t
\timmediate = {
\t\tcountry_event = {
\t\t\tid = TST.2
\t\t\ttitle = impostor.t
\t\t}
\t}
}

country_event = {
\tid = TST.2
\ttitle = TST.2.t
\tdesc = TST.2.d
}
"""

EVENTS_WITH_DUPES = """add_namespace = TST

country_event = {
\tid = TST.1
\ttitle = TST.1.t_first
}

country_event = {
\tid = TST.1
\ttitle = TST.1.t_second
}
"""

EVENTS_WITH_COMMENT = (
    "add_namespace = TST\n"
    "\n"
    "country_event = {\n"
    "\tid = TST.1\n"
    "\ttitle = TST.1.t # important note\n"
    "}\n"
)

EVENTS_WITH_IMPOSTOR_SANDWICH = """add_namespace = TST

country_event = {
\tid = TST.9
\ttitle = TST.9.t_before
\timmediate = {
\t\tcountry_event = {
\t\t\tid = TST.5
\t\t\ttitle = impostor_before.t
\t\t}
\t}
}

country_event = {
\tid = TST.5
\ttitle = TST.5.t_real
}

country_event = {
\tid = TST.9
\ttitle = TST.9.t_after
\timmediate = {
\t\tcountry_event = {
\t\t\tid = TST.5
\t\t\ttitle = impostor_after.t
\t\t}
\t}
}
"""

SAME_LINE_EVENTS = "country_event = { id = TST.1 } country_event = { id = TST.2 }\n"

SPRITES_WITH_IMPOSTOR = """spriteTypes = {
\tspriteType = {
\t\tname = "GFX_impostor_home"
\t\ttexturefile = "gfx/interface/impostor.dds"
\t\tsomeBlock = {
\t\t\tspriteType = {
\t\t\t\tname = "GFX_real"
\t\t\t\ttexturefile = "gfx/interface/decoy.dds"
\t\t\t}
\t\t}
\t}

\tspriteType = {
\t\tname = "GFX_real"
\t\ttexturefile = "gfx/interface/real.dds"
\t}
}
"""

SPRITES_WITH_DUPES = """spriteTypes = {
\tspriteType = {
\t\tname = "GFX_dup"
\t\ttexturefile = "gfx/interface/first.dds"
\t}

\tspriteType = {
\t\tname = "GFX_dup"
\t\ttexturefile = "gfx/interface/second.dds"
\t}
}
"""

SPRITES_WITH_COMMENT = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_commented"\n'
    '\t\ttexturefile = "gfx/interface/real.dds" # important note\n'
    "\t}\n"
    "}\n"
)

SPRITES_WITH_IMPOSTOR_SANDWICH = """spriteTypes = {
\tspriteType = {
\t\tname = "GFX_before"
\t\ttexturefile = "gfx/interface/before.dds"
\t\tsomeBlock = {
\t\t\tspriteType = {
\t\t\t\tname = "GFX_mid"
\t\t\t\ttexturefile = "gfx/interface/impostor_before.dds"
\t\t\t}
\t\t}
\t}

\tspriteType = {
\t\tname = "GFX_mid"
\t\ttexturefile = "gfx/interface/real.dds"
\t}

\tspriteType = {
\t\tname = "GFX_after"
\t\ttexturefile = "gfx/interface/after.dds"
\t\tsomeBlock = {
\t\t\tspriteType = {
\t\t\t\tname = "GFX_mid"
\t\t\t\ttexturefile = "gfx/interface/impostor_after.dds"
\t\t\t}
\t\t}
\t}
}
"""

SPRITES_WITH_BARE_AND_QUOTED_NAMES = """spriteTypes = {
\tspriteType = {
\t\tname = "GFX_quoted"
\t\ttexturefile = "gfx/interface/quoted.dds"
\t}
\tspriteType = {
\t\tname = GFX_bare
\t\ttexturefile = "gfx/interface/bare.dds"
\t}
}
"""

SPRITES_WITH_COMMENTED_AND_NESTED_PROPERTIES = """spriteTypes = {
\tspriteType = {
\t\t# name = "GFX_old"
\t\teffect = { name = "GFX_nested" texturefile = "gfx/interface/nested.dds" }
\t\tname = "GFX_actual"
\t\t# texturefile = "gfx/interface/old.dds"
\t\ttexturefile = "gfx/interface/actual.dds"
\t}
}
"""

FOCUS_WITH_DUPES = (
    "focus_tree = {\n"
    "\tfocus = {\n"
    "\t\tid = TST_dup\n"
    "\t\tx = 1\n"
    "\t}\n"
    "\tfocus = {\n"
    "\t\tid = TST_dup\n"
    "\t\tx = 9\n"
    "\t}\n"
    "}\n"
)

FOCUS_WITH_COMMENT = (
    "focus_tree = {\n\tfocus = {\n\t\tid = TST_a\n\t\tx = 7 # important note\n\t}\n}\n"
)

FOCUS_TREE_SHARED_JOINT = (
    "focus_tree = {\n\tfocus = {\n\t\tid = TST_tree\n\t\tx = 1\n\t}\n}\n"
    "shared_focus = {\n\tid = TST_shared\n\tx = 2\n}\n"
    "joint_focus = {\n\tid = TST_joint\n\tx = 3\n}\n"
)

SAME_LINE_DUPES = {
    "focus": "focus_tree = { focus = { id = TST_d } focus = { id = TST_d } }\n",
    "decision": "TST_category = { TST_d = { cost = 10 } TST_d = { cost = 20 } }\n",
    "event": (
        "add_namespace = TST\n\n"
        "country_event = { id = TST.1 title = a } country_event = { id = TST.1 title = b }\n"
    ),
    "sprite": (
        'spriteTypes = { spriteType = { name = "GFX_d" texturefile = "a.dds" } '
        'spriteType = { name = "GFX_d" texturefile = "b.dds" } }\n'
    ),
    "idea": "ideas = { country = { TST_d = { picture = x } TST_d = { picture = y } } }\n",
}


@pytest.mark.parametrize(
    ("kind", "text", "ident", "expected"),
    [
        pytest.param(
            "decision",
            DECISIONS_WITH_COMMENT,
            "TST_commented",
            "\tTST_commented = {\n\t\tcost = 30 # important note\n\t}",
            id="decision-keeps-comment",
        ),
        pytest.param(
            "event",
            EVENTS_WITH_COMMENT,
            "TST.1",
            "country_event = {\n\tid = TST.1\n\ttitle = TST.1.t # important note\n}",
            id="event-keeps-comment",
        ),
        pytest.param(
            "sprite",
            SPRITES_WITH_COMMENT,
            "GFX_commented",
            '\tspriteType = {\n\t\tname = "GFX_commented"\n'
            '\t\ttexturefile = "gfx/interface/real.dds" # important note\n\t}',
            id="sprite-keeps-comment",
        ),
        pytest.param(
            "decision",
            DECISION_AFTER_SAME_LINE_OPENER,
            "TST_dec",
            "TST_dec = { # opening note\n\tcost = 1 # trailing note\n\n\t# interior note\n}",
            id="decision-multiline-after-same-line-opener",
        ),
        pytest.param(
            "decision",
            "TST_category = { TST_dec = { cost = 1 } }\n",
            "TST_dec",
            "TST_dec = { cost = 1 }",
            id="decision-same-line-container-excludes-opener",
        ),
        pytest.param(
            "idea",
            "ideas = { country = { TST_idea = { picture = x } } }\n",
            "TST_idea",
            "TST_idea = { picture = x }",
            id="idea-same-line-container-excludes-openers",
        ),
        pytest.param(
            "sprite",
            'spriteTypes = { spriteType = { name = "GFX_a" texturefile = "a.dds" } }\n',
            "GFX_a",
            'spriteType = { name = "GFX_a" texturefile = "a.dds" }',
            id="sprite-same-line-container-excludes-opener",
        ),
        pytest.param(
            "event",
            SAME_LINE_EVENTS,
            "TST.1",
            "country_event = { id = TST.1 }",
            id="event-first-of-same-line-siblings",
        ),
        pytest.param(
            "event",
            SAME_LINE_EVENTS,
            "TST.2",
            "country_event = { id = TST.2 }",
            id="event-second-of-same-line-siblings",
        ),
        pytest.param(
            "event",
            "country_event = {\n\tid = TST.1\n}\n",
            "TST.1",
            "country_event = {\n\tid = TST.1\n}",
            id="event-at-start-of-file",
        ),
        pytest.param(
            "focus",
            FOCUS_WITH_DUPES,
            "TST_dup",
            "\tfocus = {\n\t\tid = TST_dup\n\t\tx = 9\n\t}",
            id="focus-duplicate-ids-anchored-by-indexed-line",
        ),
        pytest.param(
            "focus",
            FOCUS_TREE_SHARED_JOINT,
            "TST_tree",
            "\tfocus = {\n\t\tid = TST_tree\n\t\tx = 1\n\t}",
            id="focus-in-tree",
        ),
        pytest.param(
            "focus",
            FOCUS_TREE_SHARED_JOINT,
            "TST_shared",
            "shared_focus = {\n\tid = TST_shared\n\tx = 2\n}",
            id="shared-focus",
        ),
        pytest.param(
            "focus",
            FOCUS_TREE_SHARED_JOINT,
            "TST_joint",
            "joint_focus = {\n\tid = TST_joint\n\tx = 3\n}",
            id="joint-focus",
        ),
    ],
)
def test_resource_returns_exact_source(tmp_path, kind, text, ident, expected):
    assert _read(tmp_path, kind, text, ident) == expected


@pytest.mark.parametrize(
    ("kind", "text", "ident", "line", "wanted", "unwanted"),
    [
        # A same-named block nested inside another definition is never a candidate.
        ("decision", DECISIONS_WITH_IMPOSTOR, "TST_real", INDEXED, "cost = 25", ["some_effect"]),
        (
            "idea",
            IDEAS_WITH_IMPOSTOR,
            "TST_real",
            INDEXED,
            "war_support_factor = 0.05",
            ["stability_factor"],
        ),
        ("event", EVENTS_WITH_IMPOSTOR, "TST.2", INDEXED, "title = TST.2.t", ["impostor.t"]),
        ("event", EVENTS_WITH_IMPOSTOR, "TST.2", None, "title = TST.2.t", ["impostor.t"]),
        ("sprite", SPRITES_WITH_IMPOSTOR, "GFX_real", INDEXED, "real.dds", ["decoy.dds"]),
        ("sprite", SPRITES_WITH_IMPOSTOR, "GFX_real", None, "real.dds", ["decoy.dds"]),
        (
            "event",
            EVENTS_WITH_IMPOSTOR_SANDWICH,
            "TST.5",
            INDEXED,
            "title = TST.5.t_real",
            ["impostor_before", "impostor_after"],
        ),
        (
            "sprite",
            SPRITES_WITH_IMPOSTOR_SANDWICH,
            "GFX_mid",
            INDEXED,
            "real.dds",
            ["impostor_before.dds", "impostor_after.dds"],
        ),
        (
            "sprite",
            SPRITES_WITH_IMPOSTOR_SANDWICH,
            "GFX_mid",
            None,
            "real.dds",
            ["impostor_before.dds", "impostor_after.dds"],
        ),
        # The index keys the last duplicate in file order; the resource must match it.
        ("decision", DECISIONS_WITH_DUPES, "TST_dup", INDEXED, "cost = 20", ["cost = 10"]),
        ("event", EVENTS_WITH_DUPES, "TST.1", INDEXED, "TST.1.t_second", ["t_first"]),
        ("sprite", SPRITES_WITH_DUPES, "GFX_dup", INDEXED, "second.dds", ["first.dds"]),
        # One match needs no line to disambiguate.
        ("decision", DECISIONS_WITH_COMMENT, "TST_commented", None, "cost = 30", []),
        ("focus", FOCUS_WITH_COMMENT, "TST_a", None, "x = 7 # important note", []),
        ("sprite", SPRITES_WITH_BARE_AND_QUOTED_NAMES, "GFX_quoted", INDEXED, "quoted.dds", []),
        ("sprite", SPRITES_WITH_BARE_AND_QUOTED_NAMES, "GFX_bare", INDEXED, "bare.dds", []),
    ],
)
def test_resource_picks_the_indexed_definition(tmp_path, kind, text, ident, line, wanted, unwanted):
    result = _read(tmp_path, kind, text, ident, line)

    assert wanted in result
    for fragment in unwanted:
        assert fragment not in result


@pytest.mark.parametrize(
    ("kind", "text", "ident", "line", "match"),
    [
        ("decision", DECISIONS_WITH_DUPES, "TST_dup", None, "ambiguous.*no line"),
        ("event", EVENTS_WITH_DUPES, "TST.1", None, "ambiguous.*no line"),
        ("sprite", SPRITES_WITH_DUPES, "GFX_dup", None, "ambiguous.*no line"),
        ("focus", FOCUS_WITH_DUPES, "TST_dup", None, "ambiguous.*no line"),
        # Two definitions on the indexed line: the index cannot tell them apart.
        ("decision", SAME_LINE_DUPES["decision"], "TST_d", INDEXED, "ambiguous.*on line 1 "),
        ("event", SAME_LINE_DUPES["event"], "TST.1", INDEXED, "ambiguous.*on line 3 "),
        ("sprite", SAME_LINE_DUPES["sprite"], "GFX_d", INDEXED, "ambiguous.*on line 1 "),
        ("idea", SAME_LINE_DUPES["idea"], "TST_d", INDEXED, "ambiguous.*on line 1 "),
        ("focus", SAME_LINE_DUPES["focus"], "TST_d", INDEXED, "ambiguous.*on line 1 "),
        # Line 1 is the container or namespace line, where no definition starts.
        ("decision", DECISIONS_WITH_COMMENT, "TST_commented", 1, r"stale.*build-index"),
        ("event", EVENTS_WITH_COMMENT, "TST.1", 1, r"stale.*build-index"),
        ("sprite", SPRITES_WITH_COMMENT, "GFX_commented", 1, r"stale.*build-index"),
        ("focus", FOCUS_WITH_COMMENT, "TST_a", 1, r"stale.*build-index"),
    ],
)
def test_resource_rejects_a_record_it_cannot_anchor(tmp_path, kind, text, ident, line, match):
    with pytest.raises(KeyError, match=match):
        _read(tmp_path, kind, text, ident, line)


def test_focus_resource_disappeared_id_raises_stale(tmp_path):
    mod_root, settings, index = _index(tmp_path, "focus", FOCUS_WITH_COMMENT)
    rec = index.resolve("TST_a")
    assert rec is not None
    # Freeze the record while the file is rewritten without the id at all.
    (mod_root / KINDS["focus"].path).write_text(
        "focus_tree = {\n\tfocus = {\n\t\tid = TST_other\n\t}\n}\n", encoding="utf-8"
    )

    with pytest.raises(KeyError, match=r"stale.*build-index"):
        KINDS["focus"].resource("TST_a", settings, _FakeIndex(rec))


def test_sprite_index_and_resource_agree_on_direct_properties(tmp_path):
    _, settings, index = _index(tmp_path, "sprite", SPRITES_WITH_COMMENTED_AND_NESTED_PROPERTIES)

    actual = index.resolve("GFX_actual")
    assert actual is not None
    assert actual["texturefile"] == "gfx/interface/actual.dds"
    assert index.resolve("GFX_old") is None
    assert index.resolve("GFX_nested") is None
    assert 'name = "GFX_actual"' in sprite_resource("GFX_actual", settings, index)


@pytest.mark.parametrize("kind", KINDS)
def test_resource_reports_an_id_the_index_lacks(tmp_path, kind):
    settings = mod_settings(tmp_path, tmp_path / ".cache")
    index = KINDS[kind].index(tmp_path, settings.cache_dir)

    with pytest.raises(KeyError, match="'TST_absent' not found"):
        KINDS[kind].resource("TST_absent", settings, index)


@pytest.mark.parametrize("kind", KINDS)
def test_resource_reports_an_indexed_file_missing_on_disk(tmp_path, kind):
    settings = mod_settings(tmp_path, tmp_path / ".cache")
    index = _FakeIndex({"file": "common/gone.txt", "line": 1})

    with pytest.raises(FileNotFoundError, match="common/gone.txt"):
        KINDS[kind].resource("TST_gone", settings, index)


def test_loc_resource_returns_the_value_or_reports_a_missing_key(fake_mod_root, cache_dir):
    settings = mod_settings(fake_mod_root, cache_dir)
    index = LocalisationIndex(fake_mod_root, cache_dir)

    assert loc_resource("TST_root", settings, index) == "The Root Focus"
    with pytest.raises(KeyError, match="'TST_absent' not found"):
        loc_resource("TST_absent", settings, index)


@pytest.mark.parametrize(
    ("text", "ident", "expected"),
    [
        pytest.param(
            "focus_tree = { focus = { id = TST_a } }\n",
            "TST_a",
            "focus = { id = TST_a }",
            id="same-line-container-excludes-opener",
        ),
        pytest.param(
            "focus_tree = {\n \t  focus = {\n\t\tid = TST_a\n\t}\n}\n",
            "TST_a",
            " \t  focus = {\n\t\tid = TST_a\n\t}",
            id="keeps-leading-indentation",
        ),
        pytest.param(
            "focus_tree = { focus = { # opening note\n"
            "\tid = TST_a\n"
            "\n"
            "\t# interior note\n"
            "\tx = 1 # trailing note\n"
            "} }\n",
            "TST_a",
            "focus = { # opening note\n"
            "\tid = TST_a\n"
            "\n"
            "\t# interior note\n"
            "\tx = 1 # trailing note\n"
            "}",
            id="keeps-comments-after-same-line-opener",
        ),
        pytest.param(
            "shared_focus = {\n\tid = TST_s\n}\n",
            "TST_s",
            "shared_focus = {\n\tid = TST_s\n}",
            id="start-of-file-has-no-prefix",
        ),
    ],
)
def test_extract_focus_block(text, ident, expected):
    assert _extract_focus_block(text, ident) == expected


def test_extract_focus_block_malformed_node_raises_with_focus_id(monkeypatch):
    focus_id = "TST_malformed"
    malformed_focus = Node(
        name="focus",
        value=[Node(name="id", value=SymbolNode(focus_id))],
    )
    root = Node(value=[Node(name="focus_tree", value=[malformed_focus])])
    monkeypatch.setattr("md_mcp.resources.parse_string", lambda _text: root)

    with pytest.raises(KeyError, match=rf"Focus '{focus_id}'.*malformed parse"):
        _extract_focus_block("", focus_id)


def test_slice_node_rejects_a_node_without_positions():
    with pytest.raises(KeyError, match="malformed parse"):
        _slice_node("", Node(name="TST_malformed", value=[]))
