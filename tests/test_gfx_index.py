"""Unit tests for GfxIndex / _scan_sprite_blocks (issues #85 and #154)."""

import pytest

from md_mcp.indexes.gfx import _SWEEP_RE, _scan_sprite_blocks
from md_mcp.paradox import parse_string
from md_mcp.paradox.schema import extract_sprite_records

SANDWICH = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_before"\n'
    '\t\ttexturefile = "gfx/interface/before.dds"\n'
    "\t\tsomeBlock = {\n"
    "\t\t\tspriteType = {\n"
    '\t\t\t\tname = "GFX_mid"\n'
    '\t\t\t\ttexturefile = "gfx/interface/impostor_before.dds"\n'
    "\t\t\t}\n"
    "\t\t}\n"
    "\t}\n"
    "\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_mid"\n'
    '\t\ttexturefile = "gfx/interface/real.dds"\n'
    "\t}\n"
    "\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_after"\n'
    '\t\ttexturefile = "gfx/interface/after.dds"\n'
    "\t\tsomeBlock = {\n"
    "\t\t\tspriteType = {\n"
    '\t\t\t\tname = "GFX_mid"\n'
    '\t\t\t\ttexturefile = "gfx/interface/impostor_after.dds"\n'
    "\t\t\t}\n"
    "\t\t}\n"
    "\t}\n"
    "}\n"
)


def test_scanner_excludes_deeply_nested_sprites():
    recs = _scan_sprite_blocks(SANDWICH)
    names = [r["name"] for r in recs]
    assert names == ["GFX_before", "GFX_mid", "GFX_after"]


def test_scanner_indexes_real_texture_not_impostor_after():
    recs = _scan_sprite_blocks(SANDWICH)
    mid = next(r for r in recs if r["name"] == "GFX_mid")
    assert mid["texturefile"] == "gfx/interface/real.dds"


@pytest.mark.parametrize(
    ("backslash_count", "expected_matches"),
    [(1, ['"value\\"{inside"']), (2, ['"value\\\\"', "{"])],
)
def test_gfx_sweep_uses_lexer_quote_boundary_parity(backslash_count, expected_matches):
    text = '"value' + "\\" * backslash_count + '"{inside"'
    assert [match[0] for match in _SWEEP_RE.finditer(text)] == expected_matches


NESTED_CONTAINER = (
    "spriteTypes = {\n"
    '\tspriteType = {\n\t\tname = "GFX_real"\n\t}\n'
    "\totherBlock = {\n"
    "\t\tspriteTypes = {\n"
    '\t\t\tspriteType = {\n\t\t\t\tname = "GFX_fake"\n\t\t\t}\n'
    "\t\t}\n"
    "\t}\n"
    "}\n"
)


def test_scanner_excludes_nested_sprite_types_containers():
    recs = _scan_sprite_blocks(NESTED_CONTAINER)
    assert [r["name"] for r in recs] == ["GFX_real"]


# Issue #154: only direct properties outside comments and strings name a sprite.

COMMENTED_PROPERTIES = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    '\t\t# name = "GFX_old"\n'
    '\t\t# texturefile = "gfx/interface/old.dds"\n'
    '\t\tname = "GFX_actual" # name = "GFX_trailing"\n'
    '\t\ttexturefile = "gfx/interface/actual.dds" # texturefile = "trailing.dds"\n'
    "\t}\n"
    "\tspriteType = {\n"
    '\t\t#texturefile = "gfx/interface/old.dds"\n'
    '\t\tname = "GFX_uncommented_missing_texture"\n'
    "\t}\n"
    "}\n"
)

NESTED_PROPERTIES = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    "\t\tanimation = {\n"
    '\t\t\tname = "GFX_nested"\n'
    '\t\t\ttexturefile = "gfx/interface/nested.dds"\n'
    "\t\t}\n"
    '\t\tname = "GFX_outer"\n'
    '\t\ttexturefile = "gfx/interface/outer.dds"\n'
    "\t}\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_nested_texture_only"\n'
    '\t\teffect = { texturefile = "gfx/interface/nested_only.dds" }\n'
    "\t}\n"
    "\tspriteType = {\n"
    '\t\teffect = { name = "GFX_nested_name_only" }\n'
    '\t\ttexturefile = "gfx/interface/no_direct_name.dds"\n'
    "\t}\n"
    "\tcorneredTileSpriteType = {\n"
    '\t\tname = "GFX_cornered"\n'
    '\t\ttexturefile = "gfx/interface/cornered.dds"\n'
    "\t}\n"
    "}\n"
)

STRING_INTERIORS = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    '\t\tnote = "name = \\"GFX_fake\\" texturefile = \\"fake.dds\\" # { }"\n'
    '\t\tname = "GFX_real"\n'
    '\t\ttexturefile = "gfx/interface/real.dds"\n'
    "\t}\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_name = \\"GFX_inner\\" texturefile = fake"\n'
    '\t\ttexturefile = "gfx/name = x.dds"\n'
    "\t}\n"
    "}\n"
)

BARE_AND_ESCAPED_VALUES = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    "\t\tname = GFX_bare\n"
    "\t\ttexturefile = gfx/interface/bare.dds\n"
    "\t}\n"
    "\tspriteType = {\n"
    "\t\tname = GFX_bare_first\n"
    '\t\tname = "GFX_quoted_second"\n'
    "\t\ttexturefile = gfx/interface/bare_first.dds\n"
    '\t\ttexturefile = "gfx/interface/quoted_second.dds"\n'
    "\t}\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_\\"escaped\\""\n'
    '\t\ttexturefile = "gfx/interface/say \\"hi\\" \\\\ here.dds"\n'
    "\t}\n"
    "\tspriteType = {\n"
    '\t\tname = ""\n'
    '\t\ttexturefile = ""\n'
    "\t}\n"
    "}\n"
)

DUPLICATE_PROPERTIES = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_first"\n'
    '\t\tname = "GFX_second"\n'
    '\t\ttexturefile = "gfx/interface/first.dds"\n'
    '\t\ttexturefile = "gfx/interface/second.dds"\n'
    "\t}\n"
    "}\n"
)

LOOKALIKE_KEYS = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    '\t\tfoo.name = "GFX_dotted"\n'
    '\t\tmy_name = "GFX_prefixed"\n'
    '\t\tnames = "GFX_plural"\n'
    '\t\tanimationtexturefile = "gfx/interface/animation.dds"\n'
    '\t\tfoo.texturefile = "gfx/interface/dotted.dds"\n'
    '\t\tNAME = "GFX_real"\n'
    '\t\tTextureFile = "gfx/interface/real.dds"\n'
    "\t}\n"
    "}\n"
)

NON_SCALAR_NAME = (
    "spriteTypes = {\n"
    "\tspriteType = {\n"
    "\t\tname = { x = 1 }\n"
    '\t\tname = "GFX_after_block"\n'
    '\t\ttexturefile = "gfx/interface/skipped.dds"\n'
    "\t}\n"
    "\tspriteType = {\n"
    "\t\tname = 12\n"
    '\t\tname = "GFX_after_number"\n'
    "\t}\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_block_texture"\n'
    "\t\ttexturefile = { x = 1 }\n"
    '\t\ttexturefile = "gfx/interface/ignored.dds"\n'
    "\t}\n"
    "\tspriteType = {\n"
    '\t\tname = "GFX_survivor"\n'
    '\t\ttexturefile = "gfx/interface/survivor.dds"\n'
    "\t}\n"
    "}\n"
)

FIXTURES = {
    "sandwich": SANDWICH,
    "nested_container": NESTED_CONTAINER,
    "commented": COMMENTED_PROPERTIES,
    "nested": NESTED_PROPERTIES,
    "string_interiors": STRING_INTERIORS,
    "bare_and_escaped": BARE_AND_ESCAPED_VALUES,
    "duplicates": DUPLICATE_PROPERTIES,
    "lookalike_keys": LOOKALIKE_KEYS,
    "non_scalar_name": NON_SCALAR_NAME,
}


def test_scanner_ignores_commented_name_and_texturefile():
    recs = _scan_sprite_blocks(COMMENTED_PROPERTIES)
    assert [(r["name"], r["texturefile"]) for r in recs] == [
        ("GFX_actual", "gfx/interface/actual.dds"),
        ("GFX_uncommented_missing_texture", None),
    ]


def test_scanner_ignores_nested_name_and_texturefile():
    recs = _scan_sprite_blocks(NESTED_PROPERTIES)
    assert [(r["name"], r["kind"], r["texturefile"]) for r in recs] == [
        ("GFX_outer", "spriteType", "gfx/interface/outer.dds"),
        ("GFX_nested_texture_only", "spriteType", None),
        ("GFX_cornered", "corneredTileSpriteType", "gfx/interface/cornered.dds"),
    ]


def test_scanner_ignores_property_text_inside_string_values():
    recs = _scan_sprite_blocks(STRING_INTERIORS)
    assert [(r["name"], r["texturefile"]) for r in recs] == [
        ("GFX_real", "gfx/interface/real.dds"),
        ('GFX_name = "GFX_inner" texturefile = fake', "gfx/name = x.dds"),
    ]


def test_scanner_reads_bare_and_escaped_values():
    recs = _scan_sprite_blocks(BARE_AND_ESCAPED_VALUES)
    assert [(r["name"], r["texturefile"]) for r in recs] == [
        ("GFX_bare", "gfx/interface/bare.dds"),
        ("GFX_bare_first", "gfx/interface/bare_first.dds"),
        ('GFX_"escaped"', 'gfx/interface/say "hi" \\ here.dds'),
        ("", ""),
    ]


def test_scanner_first_direct_property_wins():
    recs = _scan_sprite_blocks(DUPLICATE_PROPERTIES)
    assert [(r["name"], r["texturefile"]) for r in recs] == [
        ("GFX_first", "gfx/interface/first.dds"),
    ]


def test_scanner_requires_whole_key_match_case_insensitively():
    recs = _scan_sprite_blocks(LOOKALIKE_KEYS)
    assert [(r["name"], r["texturefile"]) for r in recs] == [
        ("GFX_real", "gfx/interface/real.dds"),
    ]


def test_scanner_skips_sprite_whose_first_name_is_not_a_string_or_symbol():
    recs = {r["name"]: r for r in _scan_sprite_blocks(NON_SCALAR_NAME)}
    assert recs.keys() == {"GFX_block_texture", "GFX_survivor"}
    assert recs["GFX_block_texture"]["texturefile"] is None


@pytest.mark.parametrize("fixture", FIXTURES.values(), ids=FIXTURES.keys())
def test_scanner_matches_ast_records(fixture):
    keys = ("name", "kind", "texturefile", "line")
    expected = extract_sprite_records(parse_string(fixture), source=fixture)
    actual = _scan_sprite_blocks(fixture)
    assert [{k: r[k] for k in keys} for r in actual] == [{k: r[k] for k in keys} for r in expected]
