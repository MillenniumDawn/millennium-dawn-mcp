"""Tests for resolve_sprite_tool vanilla-manifest fallback (issue #23)."""

from __future__ import annotations

from pathlib import Path

import pytest

from md_mcp.analysis.vanilla_manifest import load_sprite_manifest
from md_mcp.config import Settings
from md_mcp.indexes import GfxIndex
from md_mcp.tools.resolver_tools import resolve_sprite_tool


def _settings(root: Path, cache: Path) -> Settings:
    return Settings(mod_root=root, vanilla_path=None, cache_dir=cache)


def test_indexed_sprite_wins_over_manifest(fake_mod_root, cache_dir) -> None:
    idx = GfxIndex(fake_mod_root, cache_dir, None)
    out = resolve_sprite_tool("GFX_test_sprite_one", _settings(fake_mod_root, cache_dir), idx)
    assert out["ok"] is True
    assert out["file"] is not None  # from the real .gfx index, not the manifest


def test_manifest_fallback_resolves_vanilla_only_sprite(fake_mod_root, cache_dir) -> None:
    idx = GfxIndex(fake_mod_root, cache_dir, None)
    vanilla_sprites = frozenset({"GFX_vanilla_icon", "GFX_test_sprite_one"})
    out = resolve_sprite_tool(
        "GFX_vanilla_icon", _settings(fake_mod_root, cache_dir), idx, vanilla_sprites
    )
    assert out["ok"] is True
    assert out["source"] == "vanilla_manifest"
    assert out["file"] is None


def test_missing_sprite_still_not_found(fake_mod_root, cache_dir) -> None:
    idx = GfxIndex(fake_mod_root, cache_dir, None)
    out = resolve_sprite_tool(
        "GFX_nope", _settings(fake_mod_root, cache_dir), idx, frozenset({"GFX_other"})
    )
    assert out["ok"] is False


def test_no_manifest_means_no_fallback(fake_mod_root, cache_dir) -> None:
    idx = GfxIndex(fake_mod_root, cache_dir, None)
    out = resolve_sprite_tool("GFX_vanilla_icon", _settings(fake_mod_root, cache_dir), idx, None)
    assert out["ok"] is False


@pytest.mark.integration
def test_manifest_fallback_against_real_mod(real_mod_root, tmp_path) -> None:
    """A vanilla-only sprite resolves from the real committed manifest (no install)."""
    cache = tmp_path / ".md-mcp-cache"
    idx = GfxIndex(real_mod_root, cache, None)
    manifest = load_sprite_manifest(real_mod_root)
    assert manifest is not None
    out = resolve_sprite_tool("GFX_3d_model_bg_air", _settings(real_mod_root, cache), idx, manifest)
    assert out["ok"] is True
    assert out["source"] == "vanilla_manifest"
