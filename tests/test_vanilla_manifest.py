"""Tests for the vanilla manifests fallback (issue #23)."""

from __future__ import annotations

from pathlib import Path

import pytest

from md_mcp.analysis.vanilla_manifest import load_manifest, load_sprite_manifest


def test_load_manifest_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    root = tmp_path / "Mod"
    (root / "tools" / "validation").mkdir(parents=True)
    (root / "tools" / "validation" / "vanilla_sprites.txt").write_text(
        "# Vanilla Hearts of Iron IV GFX sprite names\n"
        "#\n"
        "# Regenerate after a HOI4 version bump\n"
        "\n"
        "GFX_foo\n"
        "GFX_bar\n"
        "GFX_baz\n",
        encoding="utf-8",
    )
    assert load_sprite_manifest(root) == frozenset({"GFX_foo", "GFX_bar", "GFX_baz"})


def test_load_manifest_strips_whitespace(tmp_path: Path) -> None:
    root = tmp_path / "Mod"
    (root / "tools" / "validation").mkdir(parents=True)
    (root / "tools" / "validation" / "vanilla_sprites.txt").write_text(
        "  GFX_padded  \n", encoding="utf-8"
    )
    assert load_sprite_manifest(root) == frozenset({"GFX_padded"})


def test_load_manifest_missing_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "Mod"
    (root / "tools" / "validation").mkdir(parents=True)
    assert load_sprite_manifest(root) is None


def test_load_manifest_generalises_to_other_manifests(tmp_path: Path) -> None:
    root = tmp_path / "Mod"
    (root / "tools" / "validation").mkdir(parents=True)
    (root / "tools" / "validation" / "vanilla_paths.txt").write_text(
        "common/abilities/CHI_abilities.txt\n", encoding="utf-8"
    )
    assert load_manifest(root, "vanilla_paths.txt") == frozenset(
        {"common/abilities/CHI_abilities.txt"}
    )


def test_load_manifest_rejects_unknown_filename(tmp_path: Path) -> None:
    root = tmp_path / "Mod"
    (root / "tools" / "validation").mkdir(parents=True)
    with pytest.raises(ValueError):
        load_manifest(root, "../../../etc/passwd")


def test_load_manifest_empty_returns_empty_set(tmp_path: Path) -> None:
    """A comment-only manifest is present but empty — must be an empty set, not None."""
    root = tmp_path / "Mod"
    (root / "tools" / "validation").mkdir(parents=True)
    (root / "tools" / "validation" / "vanilla_sprites.txt").write_text(
        "# no entries yet\n", encoding="utf-8"
    )
    assert load_sprite_manifest(root) == frozenset()


def test_load_manifest_unreadable_returns_none(tmp_path: Path, monkeypatch) -> None:
    """A read error surfaces as "no manifest" so callers can degrade gracefully."""
    root = tmp_path / "Mod"
    (root / "tools" / "validation").mkdir(parents=True)
    (root / "tools" / "validation" / "vanilla_sprites.txt").write_text("GFX_x\n", encoding="utf-8")
    import builtins

    real_open = builtins.open

    def _raising_open(path, *args, **kwargs):
        if str(path).endswith("vanilla_sprites.txt"):
            raise OSError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _raising_open)
    assert load_sprite_manifest(root) is None
