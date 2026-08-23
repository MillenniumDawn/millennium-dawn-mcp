"""Unit tests for GfxIndex / _scan_sprite_blocks (issue #85 regression)."""

from md_mcp.indexes.gfx import _scan_sprite_blocks

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
