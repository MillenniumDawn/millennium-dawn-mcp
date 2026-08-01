"""Discovery + config tests."""

from __future__ import annotations

import pytest

from md_mcp import config
from md_mcp.util.pathing import (
    ModRootNotFound,
    find_mod_root,
    find_vanilla_path,
    resolve_scope_file,
)


def test_explicit_mod_root_wins(fake_mod_root):
    assert find_mod_root(explicit=str(fake_mod_root)) == fake_mod_root


def test_explicit_invalid_path_falls_through(tmp_path):
    with pytest.raises(ModRootNotFound) as e:
        find_mod_root(explicit=str(tmp_path), start=tmp_path)
    assert "explicit --mod-root" in str(e.value)


def test_walk_up_from_subdir(fake_mod_root):
    subdir = fake_mod_root / "common" / "national_focus"
    found = find_mod_root(start=subdir)
    assert found == fake_mod_root


def test_no_match_raises_with_actionable_message(tmp_path):
    with pytest.raises(ModRootNotFound) as e:
        find_mod_root(start=tmp_path)
    msg = str(e.value)
    assert "MD_MOD_ROOT" in msg
    assert "walk-up" in msg


def test_vanilla_path_via_env(tmp_path, monkeypatch):
    vanilla = tmp_path / "Hearts of Iron IV"
    vanilla.mkdir()
    monkeypatch.setenv("HOI4_PATH", str(vanilla))
    assert find_vanilla_path(tmp_path / "mod") == vanilla


def test_vanilla_path_sibling_auto_detect(tmp_path):
    vanilla = tmp_path / "Hearts of Iron IV"
    vanilla.mkdir()
    mod = tmp_path / "mod"
    mod.mkdir()
    assert find_vanilla_path(mod) == vanilla


def test_vanilla_path_absent(tmp_path):
    assert find_vanilla_path(tmp_path) is None


def test_resolve_scope_file_finds_mod_file(tmp_path):
    target = tmp_path / "common" / "national_focus" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    got = resolve_scope_file("common/national_focus/a.txt", tmp_path, None)
    assert got is not None
    assert got.read_text(encoding="utf-8") == "x"


def test_resolve_scope_file_falls_back_to_vanilla(tmp_path):
    mod = tmp_path / "mod"
    vanilla = tmp_path / "vanilla"
    target = vanilla / "common" / "national_focus" / "v.txt"
    target.parent.mkdir(parents=True)
    target.write_text("v", encoding="utf-8")
    mod.mkdir()
    got = resolve_scope_file("common/national_focus/v.txt", mod, vanilla)
    assert got is not None
    assert got.read_text(encoding="utf-8") == "v"


def test_resolve_scope_file_rejects_absolute_path(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    mod = tmp_path / "mod"
    mod.mkdir()
    assert resolve_scope_file(str(outside), mod, None) is None


def test_resolve_scope_file_rejects_parent_traversal(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    mod = tmp_path / "mod"
    mod.mkdir()
    assert resolve_scope_file("../outside.txt", mod, None) is None
    assert resolve_scope_file("sub/../../outside.txt", mod, None) is None


def test_resolve_scope_file_traversal_not_reachable_via_vanilla(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    mod = tmp_path / "mod"
    vanilla = tmp_path / "vanilla"
    mod.mkdir()
    vanilla.mkdir()
    assert resolve_scope_file("../outside.txt", mod, vanilla) is None


def test_resolve_scope_file_rejects_symlink_loop(tmp_path):
    mod = tmp_path / "mod"
    mod.mkdir()
    (mod / "loop").symlink_to("loop", target_is_directory=True)
    assert resolve_scope_file("loop/file.txt", mod, None) is None


def test_malformed_config_file_fails_loudly(tmp_path, monkeypatch):
    bad = tmp_path / "config.toml"
    bad.write_text("this is not = valid = toml", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", bad)
    with pytest.raises(RuntimeError, match="Could not load configuration file"):
        config._load_file_config()
