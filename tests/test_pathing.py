"""Discovery + config tests."""

from __future__ import annotations

import pytest

from md_mcp import config
from md_mcp.util.pathing import (
    ModRootNotFound,
    PathAccessError,
    find_mod_root,
    resolve_scope_file,
    validate_user_path,
)


def test_explicit_mod_root_wins(fake_mod_root):
    assert find_mod_root(explicit=str(fake_mod_root)) == fake_mod_root


def test_explicit_invalid_path_falls_through(tmp_path, monkeypatch):
    monkeypatch.delenv("MD_MOD_ROOT", raising=False)
    with pytest.raises(ModRootNotFound) as e:
        find_mod_root(explicit=str(tmp_path), start=tmp_path)
    assert "explicit --mod-root" in str(e.value)


def test_walk_up_from_subdir(fake_mod_root, monkeypatch):
    monkeypatch.delenv("MD_MOD_ROOT", raising=False)
    subdir = fake_mod_root / "common" / "national_focus"
    found = find_mod_root(start=subdir)
    assert found == fake_mod_root


def test_no_match_raises_with_actionable_message(tmp_path, monkeypatch):
    monkeypatch.delenv("MD_MOD_ROOT", raising=False)
    with pytest.raises(ModRootNotFound) as e:
        find_mod_root(start=tmp_path)
    msg = str(e.value)
    assert "MD_MOD_ROOT" in msg
    assert "walk-up" in msg


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


def test_resolve_scope_file_accepts_absolute_path_inside_root(tmp_path):
    # Widened alongside validate_user_path: absolute is fine when contained.
    mod = tmp_path / "mod"
    target = mod / "common" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    got = resolve_scope_file(str(target), mod, None)
    assert got == target.resolve()


def test_validate_user_path_resolves_relative_against_first_root(tmp_path):
    mod = tmp_path / "mod"
    vanilla = tmp_path / "vanilla"
    target = mod / "common" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("m", encoding="utf-8")
    vanilla.mkdir()
    got = validate_user_path("common/a.txt", [mod, vanilla])
    assert got == target.resolve()


def test_validate_user_path_accepts_absolute_inside_any_root(tmp_path):
    mod = tmp_path / "mod"
    vanilla = tmp_path / "vanilla"
    target = vanilla / "common" / "v.txt"
    target.parent.mkdir(parents=True)
    target.write_text("v", encoding="utf-8")
    mod.mkdir()
    assert validate_user_path(str(target), [mod, vanilla]) == target.resolve()


def test_validate_user_path_rejects_escape_from_all_roots(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    mod = tmp_path / "mod"
    vanilla = tmp_path / "vanilla"
    mod.mkdir()
    vanilla.mkdir()
    with pytest.raises(PathAccessError, match="outside the allowed content roots"):
        validate_user_path(str(outside), [mod, vanilla])
    with pytest.raises(PathAccessError, match="outside the allowed content roots"):
        validate_user_path("../outside.txt", [mod, vanilla])


def test_validate_user_path_rejects_symlink_escape(tmp_path):
    mod = tmp_path / "mod"
    link = mod / "link.txt"
    mod.mkdir()
    link.symlink_to(tmp_path / "outside.txt")
    with pytest.raises(PathAccessError):
        validate_user_path("link.txt", [mod])


def test_validate_user_path_require_file_and_extensions(tmp_path):
    mod = tmp_path / "mod"
    (mod / "interface").mkdir(parents=True)
    gfx = mod / "interface" / "icons.gfx"
    gfx.write_text("x", encoding="utf-8")
    (mod / "interface" / "notes.md").write_text("x", encoding="utf-8")

    got = validate_user_path("interface/icons.gfx", [mod], extensions={".txt", ".gfx"})
    assert got == gfx.resolve()

    with pytest.raises(PathAccessError, match="not a regular file"):
        validate_user_path("interface", [mod], require_file=True)
    with pytest.raises(PathAccessError, match="unsupported extension"):
        validate_user_path("interface/notes.md", [mod], extensions={".txt", ".gfx"})


def test_validate_user_path_accepts_single_root(tmp_path):
    mod = tmp_path / "mod"
    target = mod / "a.txt"
    mod.mkdir()
    target.write_text("x", encoding="utf-8")
    assert validate_user_path("a.txt", mod) == target.resolve()


def test_validate_user_path_relative_escape_into_second_root_is_accepted(tmp_path):
    # parse_file semantics: relative paths resolve against the first root only,
    # but the result is accepted if it lands inside any root.
    mod = tmp_path / "mod"
    vanilla = tmp_path / "vanilla"
    target = vanilla / "common" / "v.txt"
    target.parent.mkdir(parents=True)
    target.write_text("v", encoding="utf-8")
    mod.mkdir()
    assert validate_user_path("../vanilla/common/v.txt", [mod, vanilla]) == target.resolve()


def test_malformed_config_file_fails_loudly(tmp_path, monkeypatch):
    bad = tmp_path / "config.toml"
    bad.write_text("this is not = valid = toml", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", bad)
    with pytest.raises(RuntimeError, match="Could not load configuration file"):
        config._load_file_config()


def _make_mod_root(path):
    (path / "tools" / "validation").mkdir(parents=True)
    (path / "descriptor.mod").write_text('name = "x"\n', encoding="utf-8")
    return path


def test_env_mod_root_wins_over_config_file(tmp_path, monkeypatch):
    env_root = _make_mod_root(tmp_path / "EnvMod")
    file_root = _make_mod_root(tmp_path / "FileMod")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'mod_root = "{file_root}"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setenv("MD_MOD_ROOT", str(env_root))
    assert config.load().mod_root == env_root


def test_explicit_mod_root_wins_over_env(tmp_path, monkeypatch):
    explicit_root = _make_mod_root(tmp_path / "ExplicitMod")
    env_root = _make_mod_root(tmp_path / "EnvMod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MOD_ROOT", str(env_root))
    assert config.load(str(explicit_root)).mod_root == explicit_root


def test_unrecognized_validator_mode_fails_loudly(tmp_path, monkeypatch):
    root = _make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_VALIDATOR_MODE", "in-process")
    with pytest.raises(RuntimeError, match="in-process"):
        config.load(str(root))
