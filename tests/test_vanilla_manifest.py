"""Tests for the vanilla manifests fallback (issue #23)."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from md_mcp.analysis.vanilla_manifest import load_manifest, load_sprite_manifest


def _mod_with(tmp_path: Path, content: str | None, name: str = "vanilla_sprites.txt") -> Path:
    """A mod root whose `tools/validation/<name>` holds `content`, or nothing for None."""
    root = tmp_path / "Mod"
    (root / "tools" / "validation").mkdir(parents=True)
    if content is not None:
        (root / "tools" / "validation" / name).write_text(content, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("content", "names"),
    [
        pytest.param(
            "# Vanilla Hearts of Iron IV GFX sprite names\n"
            "#\n"
            "# Regenerate after a HOI4 version bump\n"
            "\n"
            "GFX_foo\n"
            "GFX_bar\n"
            "GFX_baz\n",
            {"GFX_foo", "GFX_bar", "GFX_baz"},
            id="skips-comments-and-blank-lines",
        ),
        # Upstream appends WxH when the texture could be read; the name is the key.
        pytest.param(
            "GFX_sized 510x210\nGFX_bare\n", {"GFX_sized", "GFX_bare"}, id="drops-texture-size"
        ),
        pytest.param("  GFX_padded  \n", {"GFX_padded"}, id="strips-whitespace"),
        # A BOM on the first comment line must not be mistaken for an entry.
        pytest.param("﻿# header comment\nGFX_real\n", {"GFX_real"}, id="tolerates-bom"),
        # A comment-only manifest is present but empty: an empty set, not None.
        pytest.param("# no entries yet\n", set(), id="comment-only-is-empty"),
    ],
)
def test_load_sprite_manifest_reads_names(tmp_path: Path, content: str, names: set[str]) -> None:
    assert load_sprite_manifest(_mod_with(tmp_path, content)) == frozenset(names)


def test_load_manifest_missing_returns_none(tmp_path: Path) -> None:
    assert load_sprite_manifest(_mod_with(tmp_path, None)) is None


def test_load_manifest_sees_rewrite_and_removal(tmp_path: Path) -> None:
    """The parsed manifest is memoized by stat, so a changed file must miss."""
    root = _mod_with(tmp_path, "GFX_old\n")
    manifest = root / "tools" / "validation" / "vanilla_sprites.txt"
    assert load_sprite_manifest(root) == frozenset({"GFX_old"})
    assert load_sprite_manifest(root) == frozenset({"GFX_old"})
    manifest.write_text("GFX_new\nGFX_newer\n", encoding="utf-8")
    assert load_sprite_manifest(root) == frozenset({"GFX_new", "GFX_newer"})
    manifest.unlink()
    assert load_sprite_manifest(root) is None


def test_load_manifest_generalises_to_other_manifests(tmp_path: Path) -> None:
    root = _mod_with(tmp_path, "common/abilities/CHI_abilities.txt\n", "vanilla_paths.txt")
    assert load_manifest(root, "vanilla_paths.txt") == frozenset(
        {"common/abilities/CHI_abilities.txt"}
    )


def test_load_manifest_rejects_unknown_filename(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_manifest(_mod_with(tmp_path, None), "../../../etc/passwd")


def test_load_manifest_unreadable_returns_none(tmp_path: Path, monkeypatch) -> None:
    """A read error surfaces as "no manifest" so callers can degrade gracefully."""
    root = _mod_with(tmp_path, "GFX_x\n")
    real_open = builtins.open

    def _raising_open(path, *args, **kwargs):
        if str(path).endswith("vanilla_sprites.txt"):
            raise OSError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _raising_open)
    assert load_sprite_manifest(root) is None
