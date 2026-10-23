"""Per-kind index parsers: what each reads from a file, and what it does with a bad one."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from md_mcp.indexes import (
    CharacterIndex,
    CountryTagIndex,
    DecisionIndex,
    EventIndex,
    GfxIndex,
    IdeaIndex,
    ScriptedEffectIndex,
    ScriptedTriggerIndex,
    TraitIndex,
)
from md_mcp.indexes.base import ParseResult
from md_mcp.indexes.decision import _parse_decision_file
from md_mcp.indexes.definitions import (
    _parse_root,
    parse_character_file,
    parse_country_tag_file,
    parse_scripted_effect_file,
    parse_scripted_trigger_file,
    parse_trait_file,
)
from md_mcp.indexes.event import _parse_event_file
from md_mcp.indexes.focus import _parse_focus_file
from md_mcp.indexes.gfx import _parse_gfx_file
from md_mcp.indexes.idea import _parse_idea_file

# (index, every key it reads from the fake mod, one key, its source file, fields it carries)
FIXTURE_RECORDS = [
    (
        GfxIndex,
        ["GFX_test_sprite_one", "GFX_test_sprite_two", "GFX_test_tile"],
        "GFX_test_sprite_one",
        "test_sprites.gfx",
        {"texturefile": "gfx/test/one.dds", "kind": "spriteType"},
    ),
    (
        EventIndex,
        ["Testns.1", "TestnsNews.42"],
        "Testns.1",
        "test_events.txt",
        {
            "kind": "country_event",
            "namespace": "Testns",
            "file_namespaces": ["Testns", "TestnsNews"],
        },
    ),
    # `icon` and `priority` inside the category are not decisions.
    (
        DecisionIndex,
        ["TST_simple_decision", "TST_targeted_decision"],
        "TST_simple_decision",
        "test_decisions.txt",
        {"category": "TST_category"},
    ),
    # `designer` inside tank_manufacturer is a slot keyword, not an idea.
    (
        IdeaIndex,
        ["TST_acme_tanks", "TST_another_idea", "TST_simple_idea"],
        "TST_acme_tanks",
        "test_ideas.txt",
        {"category": "tank_manufacturer"},
    ),
    (
        IdeaIndex,
        ["TST_acme_tanks", "TST_another_idea", "TST_simple_idea"],
        "TST_simple_idea",
        "test_ideas.txt",
        {"category": "country"},
    ),
    (
        CountryTagIndex,
        ["TST"],
        "TST",
        "test_tags.txt",
        {"kind": "country_tag", "country_file": "countries/Testland.txt"},
    ),
    (
        CharacterIndex,
        ["TST_test_character"],
        "TST_test_character",
        "TST.txt",
        {"kind": "character"},
    ),
    (TraitIndex, ["TST_test_trait"], "TST_test_trait", "TST_traits.txt", {"kind": "trait"}),
    (
        ScriptedEffectIndex,
        ["TST_test_effect"],
        "TST_test_effect",
        "test_effects.txt",
        {"kind": "scripted_effect"},
    ),
    (
        ScriptedTriggerIndex,
        ["TST_test_trigger"],
        "TST_test_trigger",
        "test_triggers.txt",
        {"kind": "scripted_trigger"},
    ),
]


@pytest.mark.parametrize(("index_cls", "keys", "key", "file_name", "fields"), FIXTURE_RECORDS)
def test_index_reads_the_fixture_mod(
    fake_mod_root, cache_dir, index_cls, keys, key, file_name, fields
):
    index = index_cls(fake_mod_root, cache_dir, include_vanilla=False)

    assert index.list_keys() == keys
    record = index.resolve(key)
    assert record is not None
    assert Path(record["file"]).name == file_name
    assert {name: record[name] for name in fields} == fields


@pytest.mark.integration
def test_real_mod_definition_indexes(real_mod_root, tmp_path):
    """Smoke-test the indexes against the history-scale sibling checkout."""
    cache = tmp_path / "cache"
    for index_cls, floor in (
        (CountryTagIndex, 100),
        (CharacterIndex, 100),
        (TraitIndex, 100),
        (ScriptedEffectIndex, 100),
        (ScriptedTriggerIndex, 50),
    ):
        index = index_cls(real_mod_root, cache, include_vanilla=False)
        assert len(index.list_keys()) > floor, index_cls.__name__


# Each tracked parser with a body that does not parse. The body carries the token
# the parser prefilters on, so the failure comes from the parse and not the prefilter.
MALFORMED = [
    (_parse_decision_file, "TST_category = {{{"),
    (_parse_event_file, "country_event = {{{"),
    (_parse_idea_file, "ideas = {{{"),
    (parse_scripted_effect_file, "TST_effect = {{{"),
    (parse_scripted_trigger_file, "TST_trigger = {{{"),
    # The sprite scanner rejects unbalanced braces and the AST fallback fails too.
    (_parse_gfx_file, "spriteTypes = { spriteType = {{{"),
    (_parse_gfx_file, "spriteTypes = { spriteType = { } } }"),
]

# These indexes do not track parse errors, so their parsers still drop the reason.
UNTRACKED_MALFORMED = [
    (parse_country_tag_file, "TST = {{{"),
    (parse_character_file, "characters = {{{"),
    (parse_trait_file, "leader_traits = {{{"),
]


@pytest.mark.parametrize(("parse", "malformed"), MALFORMED)
def test_parser_says_why_it_could_not_read_or_parse_a_file(tmp_path, parse, malformed):
    broken = tmp_path / "broken.txt"
    broken.write_text(malformed, encoding="utf-8")

    unreadable = parse(str(tmp_path / "missing.txt"), "missing.txt")
    assert unreadable.records is None
    assert unreadable.error.startswith("read failed:")
    unparsable = parse(str(broken), "broken.txt")
    assert unparsable.records is None
    assert unparsable.error.startswith("parse failed:")


@pytest.mark.parametrize(("parse", "malformed"), UNTRACKED_MALFORMED)
def test_untracked_parser_returns_none_for_a_file_it_cannot_read_or_parse(
    tmp_path, parse, malformed
):
    broken = tmp_path / "broken.txt"
    broken.write_text(malformed, encoding="utf-8")

    assert parse(str(tmp_path / "missing.txt"), "missing.txt") is None
    assert parse(str(broken), "broken.txt") is None


# The unclosed brace makes a real parse fail, so these pass only if the
# prefilter returns before parsing.
_TOKEN_FREE_MALFORMED = "some_unrelated_block = { foo = bar"


@pytest.mark.parametrize("parse", [_parse_event_file, _parse_idea_file, _parse_gfx_file])
def test_parser_skips_a_file_without_its_token(tmp_path, parse):
    f = tmp_path / "unrelated.txt"
    f.write_text(_TOKEN_FREE_MALFORMED, encoding="utf-8")

    assert parse(str(f), "unrelated.txt") == ParseResult([])


# A valid file that carries the prefilter token (decisions and scripted files have
# none) but defines nothing is empty, not failed.
EMPTY_BUT_VALID = [
    (_parse_decision_file, ""),
    (_parse_event_file, "# country_event\nadd_namespace = tst\n"),
    (_parse_idea_file, "ideas = { }\n"),
    (parse_scripted_effect_file, ""),
    (parse_scripted_trigger_file, "scripted_triggers = { }\n"),
    (_parse_gfx_file, "spriteTypes = { }\n"),
]


@pytest.mark.parametrize(("parse", "text"), EMPTY_BUT_VALID)
def test_parser_reports_a_valid_file_without_definitions_as_empty(tmp_path, parse, text):
    f = tmp_path / "empty.txt"
    f.write_text(text, encoding="utf-8")

    assert parse(str(f), "empty.txt") == ParseResult([])


def test_focus_parser_reports_a_read_failure_and_skips_a_file_without_focuses(tmp_path):
    f = tmp_path / "unrelated.txt"
    f.write_text(_TOKEN_FREE_MALFORMED, encoding="utf-8")

    missing = _parse_focus_file(str(tmp_path / "missing.txt"), "missing.txt")
    assert missing.records is None
    assert missing.error is not None and missing.error.startswith("read failed:")
    assert _parse_focus_file(str(f), "unrelated.txt") == ParseResult([])


def test_parse_root_reports_why_it_failed(tmp_path):
    broken = tmp_path / "broken.txt"
    broken.write_text("TST = {{{", encoding="utf-8")
    fine = tmp_path / "fine.txt"
    fine.write_text("TST = { }", encoding="utf-8")

    unparsable = _parse_root(str(broken), "broken.txt")
    assert isinstance(unparsable, ParseResult)
    assert unparsable.records is None
    assert unparsable.error is not None and unparsable.error.startswith("parse failed:")
    parsed = _parse_root(str(fine), "fine.txt")
    assert not isinstance(parsed, ParseResult)
    assert parsed[1] == "TST = { }"


def test_parse_root_reports_a_missing_file(tmp_path, caplog):
    """An unreadable file logs a single warning with both the path and the
    exception rendered — not a stray `--- Logging error ---` traceback from
    a mismatched format string. Regression for the missing second `%s` in
    `definition index: cannot read %s`, which made `exc` an extra positional
    that logging tried to format as `exc_info`.
    """
    missing = tmp_path / "does_not_exist.txt"
    with caplog.at_level(logging.WARNING, logger="md_mcp.indexes.definitions"):
        result = _parse_root(str(missing), "does_not_exist.txt")
    assert isinstance(result, ParseResult)
    assert result.records is None
    assert result.error is not None and result.error.startswith("read failed:")
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    # Rendered message includes both the path and the OSError reason.
    assert "does_not_exist.txt" in record.getMessage()
    # The format string must hold one `%s` per arg; the regression was a
    # single `%s` with two args, which logging rendered as a stray
    # `--- Logging error ---` instead of the warning.
    assert record.msg.count("%s") == 2
    assert len(record.args) == 2


def _ids(parse, tmp_path: Path, name: str, text: str) -> list[str]:
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    result = parse(str(f), name)
    records = result.records if isinstance(result, ParseResult) else result
    assert records is not None
    return [record["id"] for record in records]


def test_country_tag_parser_keeps_only_tag_shaped_names_with_a_file(tmp_path):
    text = (
        'TST = "countries/Testland.txt"\n'
        'lowercase = "countries/No.txt"\n'
        'TOOLONGTAG = "countries/No.txt"\n'
        "NUM = 5\n"
    )

    assert _ids(parse_country_tag_file, tmp_path, "tags.txt", text) == ["TST"]


def test_trait_parser_reads_bare_traits_only_from_a_traits_file(tmp_path):
    # Some vanilla-style trait files omit the leader_traits container.
    text = "TST_bare_trait = { random = no }\nnote = 1\n"

    assert _ids(parse_trait_file, tmp_path, "TST_traits.txt", text) == ["TST_bare_trait"]
    assert _ids(parse_trait_file, tmp_path, "leader_skills.txt", text) == []


def test_character_parser_reads_only_blocks_inside_characters(tmp_path):
    text = "characters = { TST_one = { name = One } note = 1 }\nother = { TST_two = { } }\n"

    assert _ids(parse_character_file, tmp_path, "TST.txt", text) == ["TST_one"]


# Vanilla-recovery fixtures (issue #300): the engine loads files with an unclosed
# final block; vanilla-root indexing passes close_blocks_at_eof=True, mod indexing
# stays strict. Texts are minimal stand-ins for the real vanilla files.
_IDEAS_UNCLOSED = (
    "ideas = {\n"
    "\tcountry = {\n"
    "\t\tSOV_damaged_economy = {\n"
    "\t\t\tallowed = { always = yes }\n"
    "\t\t\tcost = 1\n"
    "\t\t}\n"
    "\t\tSOV_nationalize = {\n"
    "\t\t\tallowed = { always = yes }\n"
    "\t\t\tcancel = { always = no }\n"
    "\t\t}\n"
    "\t}\n"
)


def test_vanilla_idea_file_missing_final_block_recovers_records(tmp_path):
    unclosed = tmp_path / "unclosed.txt"
    unclosed.write_text(_IDEAS_UNCLOSED, encoding="utf-8")

    parsed = _parse_idea_file(str(unclosed), "unclosed.txt", True)

    assert parsed.records is not None and parsed.error is None
    assert [r["id"] for r in parsed.records] == ["SOV_damaged_economy", "SOV_nationalize"]
    assert all(r["category"] == "country" for r in parsed.records)

    strict = _parse_idea_file(str(unclosed), "unclosed.txt", False)
    assert strict.records is None
    assert strict.error is not None and strict.error.startswith("parse failed:")


@pytest.mark.parametrize("close", [True, False])
def test_vanilla_event_file_leading_plus_and_acute_symbol_index(tmp_path, close):
    """`amount = +0.10` (ETH.txt) and `base = ´45` (WUW_Germany.txt) are global
    lexer-widening fixes: the event file indexes in both modes, unlike the
    unclosed-block recovery which is vanilla-only."""
    text = (
        "add_namespace = testns\n"
        "country_event = {\n"
        "\tid = testns.1\n"
        "\timmediate = {\n"
        "\t\tconsumer_goods_factor = +0.10\n"
        "\t\tai_chance = { base = ´45 }\n"
        "\t\tbare = {\n"
        "\t\t\t{ icon = gfx }\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )
    event = tmp_path / "events.txt"
    event.write_text(text, encoding="utf-8")

    parsed = _parse_event_file(str(event), "events.txt", close)
    assert parsed.records is not None, parsed.error
    assert [r["namespace"] for r in parsed.records] == ["testns"]
    assert parsed.records[0]["id"] == "testns.1"


@pytest.mark.parametrize("close", [True, False])
def test_vanilla_decision_file_leading_plus_decimal_indexes(tmp_path, close):
    """Mirrors vanilla common/decisions/ETH.txt: `consumer_goods_factor = +0.10`
    inside a decision's modifier must not stop the file from indexing."""
    text = (
        "TST_category = {\n"
        "\tTST_decision = {\n"
        "\t\tmodifier = {\n"
        "\t\t\tconsumer_goods_factor = +0.10\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )
    decision = tmp_path / "decisions.txt"
    decision.write_text(text, encoding="utf-8")

    parsed = _parse_decision_file(str(decision), "decisions.txt", close)
    assert parsed.records is not None, parsed.error
    assert parsed.records[0]["id"] == "TST_decision"
    assert parsed.records[0]["category"] == "TST_category"


def test_vanilla_gfx_file_missing_final_block_recovers_sprites(tmp_path):
    """interface/powerbalanceview.gfx never closes `spriteTypes`; the scanner
    rejects unbalanced braces and the AST fallback with EOF recovery indexes it.
    Mod GFX files keep the strict failure (they track real bugs)."""
    text = (
        "spriteTypes = {\n"
        "\tspriteType = {\n"
        '\t\tname = "GFX_bop_glow_effect"\n'
        '\t\ttexturefile = "gfx/interface/bop/glow.dds"\n'
        "\t}\n"
    )
    gfx = tmp_path / "powerbalanceview.gfx"
    gfx.write_text(text, encoding="utf-8")

    parsed = _parse_gfx_file(str(gfx), "powerbalanceview.gfx", True)
    assert parsed.records is not None and parsed.error is None
    assert [r["name"] for r in parsed.records] == ["GFX_bop_glow_effect"]

    strict = _parse_gfx_file(str(gfx), "powerbalanceview.gfx", False)
    assert strict.records is None
    assert strict.error is not None and strict.error.startswith("parse failed:")


def test_definition_parse_root_recovers_vanilla_unclosed_block(tmp_path):
    broken = tmp_path / "broken.txt"
    broken.write_text('TST = {\n\tcountry_file = "countries/T.txt"\n', encoding="utf-8")

    recovered = _parse_root(str(broken), "broken.txt", close_blocks_at_eof=True)
    assert not isinstance(recovered, ParseResult)
    strict = _parse_root(str(broken), "broken.txt")
    assert isinstance(strict, ParseResult)
    assert strict.error is not None and strict.error.startswith("parse failed:")


@pytest.mark.parametrize("parse", [parse_scripted_effect_file, parse_scripted_trigger_file])
def test_scripted_parser_reads_bare_and_wrapped_definitions(tmp_path, parse):
    text = "scripted_effects = { TST_wrapped = { } note = 1 }\nTST_bare = { }\nflag = yes\n"

    assert _ids(parse, tmp_path, "scripted.txt", text) == ["TST_wrapped", "TST_bare"]
