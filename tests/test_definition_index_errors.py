"""Definition indexes keep the reason a file was dropped instead of losing it (issue #156).

A file that cannot be read or parsed contributes no definitions. The index must say so,
or a caller cannot tell "this id does not exist" from "the file that defines it was
dropped". Event, idea, decision, scripted effect and trigger, and gfx indexes track the
failure the way the focus index does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from md_mcp.indexes import (
    DecisionIndex,
    EventIndex,
    GenericTxtIndex,
    GfxIndex,
    IdeaIndex,
    ScriptedEffectIndex,
    ScriptedTriggerIndex,
)
from md_mcp.util.encoding import read_text
from tests.support.indexes import (
    force_refresh,
    index_state,
    scratch_state,
    spy_parse,
    write_stepped,
)


@dataclass(frozen=True)
class Kind:
    index_cls: type[GenericTxtIndex]
    module: str  # the indexes module whose parser calls `read_text`
    subdir: str
    suffix: str
    good: str  # defines exactly `key`
    key: str
    broken: str  # does not parse
    empty: str  # parses, defines nothing


KINDS = {
    "event": Kind(
        EventIndex,
        "event",
        "events",
        ".txt",
        "add_namespace = tst\ncountry_event = { id = tst.1 }\n",
        "tst.1",
        "country_event = {{{",
        "# country_event\nadd_namespace = tst\n",
    ),
    "idea": Kind(
        IdeaIndex,
        "idea",
        "common/ideas",
        ".txt",
        "ideas = { country = { TST_idea = { picture = generic } } }\n",
        "TST_idea",
        "ideas = {{{",
        "ideas = { }\n",
    ),
    "decision": Kind(
        DecisionIndex,
        "decision",
        "common/decisions",
        ".txt",
        "TST_category = { TST_decision = { cost = 1 } }\n",
        "TST_decision",
        "TST_category = {{{",
        "",
    ),
    "scripted_effect": Kind(
        ScriptedEffectIndex,
        "definitions",
        "common/scripted_effects",
        ".txt",
        "TST_effect = { add_political_power = 1 }\n",
        "TST_effect",
        "TST_effect = {{{",
        "",
    ),
    "scripted_trigger": Kind(
        ScriptedTriggerIndex,
        "definitions",
        "common/scripted_triggers",
        ".txt",
        "scripted_triggers = { TST_trigger = { always = yes } }\n",
        "TST_trigger",
        "scripted_triggers = {{{",
        "scripted_triggers = { }\n",
    ),
    "gfx": Kind(
        GfxIndex,
        "gfx",
        "interface",
        ".gfx",
        'spriteTypes = { spriteType = { name = "GFX_ok" texturefile = "gfx/ok.dds" } }\n',
        "GFX_ok",
        "spriteTypes = { spriteType = {{{",
        "spriteTypes = { }\n",
    ),
}

every_kind = pytest.mark.parametrize("kind", KINDS.values(), ids=KINDS.keys())


def _mod(tmp_path: Path, kind: Kind, **files: str) -> Path:
    root = tmp_path / "Mod"
    for step, (name, text) in enumerate(files.items(), start=1):
        write_stepped(root / kind.subdir / f"{name}{kind.suffix}", text, step)
    return root


def _rel(kind: Kind, name: str) -> str:
    return str(Path(kind.subdir) / f"{name}{kind.suffix}")


def _index(kind: Kind, root: Path, cache: Path) -> GenericTxtIndex:
    return kind.index_cls(root, cache, include_vanilla=False)


@every_kind
def test_malformed_file_is_a_parse_error_and_its_neighbours_still_index(tmp_path, cache_dir, kind):
    index = _index(kind, _mod(tmp_path, kind, good=kind.good, bad=kind.broken), cache_dir)

    errors = index.parse_errors()

    assert [error["file"] for error in errors] == [_rel(kind, "bad")]
    assert errors[0]["error"].startswith("parse failed:")
    assert index.list_keys() == [kind.key]


@every_kind
def test_unreadable_file_is_a_read_error_that_survives_a_restart(
    tmp_path, cache_dir, monkeypatch, kind
):
    root = _mod(tmp_path, kind, good=kind.good, locked=kind.good)

    def flaky(path):
        if Path(path).name.startswith("locked"):
            raise OSError("locked")
        return read_text(path)

    monkeypatch.setattr(f"md_mcp.indexes.{kind.module}.read_text", flaky)
    first = _index(kind, root, cache_dir)
    expected = [{"file": _rel(kind, "locked"), "error": "read failed: locked"}]
    assert first.parse_errors() == expected
    assert first.list_keys() == [kind.key]

    second = _index(kind, root, cache_dir)
    parsed = spy_parse(second, monkeypatch)
    assert second.parse_errors() == expected
    assert parsed == []


@every_kind
def test_valid_file_without_definitions_is_empty_not_failed(tmp_path, cache_dir, kind):
    index = _index(kind, _mod(tmp_path, kind, good=kind.good, empty=kind.empty), cache_dir)

    assert index.parse_errors() == []
    assert index.list_keys() == [kind.key]
    assert _rel(kind, "empty") in index.list_files()
    assert index.records_for_file(_rel(kind, "empty")) == []


@every_kind
def test_cold_reload_keeps_the_error_message_without_reparsing(
    tmp_path, cache_dir, monkeypatch, kind
):
    root = _mod(tmp_path, kind, good=kind.good, bad=kind.broken)
    first = _index(kind, root, cache_dir)
    expected = first.parse_errors()
    assert expected[0]["error"].startswith("parse failed:")

    second = _index(kind, root, cache_dir)
    parsed = spy_parse(second, monkeypatch)
    assert second.parse_errors() == expected
    assert parsed == []
    assert index_state(second) == index_state(first)


@every_kind
def test_file_repaired_before_the_next_start_clears_its_cached_error(
    tmp_path, cache_dir, monkeypatch, kind
):
    root = _mod(tmp_path, kind, good=kind.good, other=kind.broken)
    first = _index(kind, root, cache_dir)
    assert [error["file"] for error in first.parse_errors()] == [_rel(kind, "other")]

    write_stepped(root / kind.subdir / f"other{kind.suffix}", kind.empty, 10)
    second = _index(kind, root, cache_dir)
    parsed = spy_parse(second, monkeypatch)
    assert second.parse_errors() == []
    assert parsed == [[_rel(kind, "other")]]
    assert index_state(second) == scratch_state(kind.index_cls, root, tmp_path, "repaired")


@every_kind
def test_warm_refresh_follows_a_file_breaking_and_being_repaired(tmp_path, cache_dir, kind):
    root = _mod(tmp_path, kind, a=kind.good, b=kind.empty)
    target = root / kind.subdir / f"a{kind.suffix}"
    index = _index(kind, root, cache_dir)
    assert index.parse_errors() == []

    write_stepped(target, kind.broken, 10)
    force_refresh(index)
    assert [error["file"] for error in index.parse_errors()] == [_rel(kind, "a")]
    assert index.list_keys() == []
    assert index_state(index) == scratch_state(kind.index_cls, root, tmp_path, "broken")

    write_stepped(target, kind.good, 11)
    force_refresh(index)
    assert index.parse_errors() == []
    assert index.list_keys() == [kind.key]
    assert index_state(index) == scratch_state(kind.index_cls, root, tmp_path, "repaired")

    write_stepped(target, kind.broken, 12)
    force_refresh(index)
    reloaded = _index(kind, root, cache_dir)
    assert reloaded.parse_errors() == index.parse_errors()
    assert reloaded.parse_errors()[0]["error"].startswith("parse failed:")


@every_kind
def test_pooled_build_returns_the_structured_error(tmp_path, cache_dir, monkeypatch, kind):
    monkeypatch.delenv("MD_MCP_SERIAL_PARSE", raising=False)
    root = _mod(tmp_path, kind, a=kind.good, b=kind.empty, c=kind.empty, bad=kind.broken)

    errors = _index(kind, root, cache_dir).parse_errors()

    assert [error["file"] for error in errors] == [_rel(kind, "bad")]
    assert errors[0]["error"].startswith("parse failed:")


@every_kind
def test_a_file_that_vanishes_before_it_is_parsed_is_reported_missing(tmp_path, cache_dir, kind):
    index = _index(kind, _mod(tmp_path, kind, good=kind.good), cache_dir)
    gone = _rel(kind, "gone")

    assert index._parse_results([gone]) == {gone: (None, "file not found")}
