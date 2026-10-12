"""Mod root discovery and path containment."""

from __future__ import annotations

import pytest

from md_mcp.util.pathing import (
    ModRootNotFound,
    PathAccessError,
    find_mod_root,
    resolve_scope_file,
    validate_user_path,
)
from tests.support.mod import make_mod_root


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


def test_env_mod_root_is_used_when_valid_and_reported_when_not(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "EnvMod")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setenv("MD_MOD_ROOT", str(root))
    assert find_mod_root(start=elsewhere) == root

    monkeypatch.setenv("MD_MOD_ROOT", str(elsewhere))
    with pytest.raises(ModRootNotFound, match="MD_MOD_ROOT env var"):
        find_mod_root(start=elsewhere)


def test_sibling_millennium_dawn_directory_is_found(tmp_path, monkeypatch):
    monkeypatch.delenv("MD_MOD_ROOT", raising=False)
    root = make_mod_root(tmp_path / "Millennium-Dawn")
    start = tmp_path / "millennium-dawn-mcp"
    start.mkdir()

    assert find_mod_root(start=start) == root


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


def test_resolve_scope_file_prefers_submod_overlay(tmp_path):
    mod = tmp_path / "mod"
    submod = tmp_path / "submod"
    vanilla = tmp_path / "vanilla"
    rel = "common/national_focus/shared.txt"
    for root, text in ((mod, "mod"), (submod, "submod"), (vanilla, "vanilla")):
        target = root / rel
        target.parent.mkdir(parents=True)
        target.write_text(text, encoding="utf-8")
    got = resolve_scope_file(rel, mod, vanilla, **{"submod_root": submod})
    assert got == (submod / rel).resolve()


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


def test_validate_user_path_prefers_first_root_with_existing_file(tmp_path):
    submod = tmp_path / "submod"
    mod = tmp_path / "mod"
    vanilla = tmp_path / "vanilla"
    for root, rel in (
        (submod, "common/shared.txt"),
        (mod, "common/shared.txt"),
        (mod, "common/base.txt"),
        (vanilla, "common/vanilla.txt"),
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x", encoding="utf-8")
    roots = [submod, mod, vanilla]

    assert (
        validate_user_path("common/shared.txt", roots) == (submod / "common/shared.txt").resolve()
    )
    assert validate_user_path("common/base.txt", roots) == (mod / "common/base.txt").resolve()
    assert (
        validate_user_path("common/vanilla.txt", roots)
        == (vanilla / "common/vanilla.txt").resolve()
    )


def test_validate_user_path_missing_everywhere_falls_back_to_first_root(tmp_path):
    submod = tmp_path / "submod"
    mod = tmp_path / "mod"
    submod.mkdir()
    mod.mkdir()

    got = validate_user_path("nope.txt", [submod, mod])
    assert got == (submod / "nope.txt").resolve()
    with pytest.raises(PathAccessError, match="not a regular file"):
        validate_user_path("nope.txt", [submod, mod], require_file=True)


def test_validate_user_path_symlink_escape_in_overlay_is_not_preferred(tmp_path):
    submod = tmp_path / "submod"
    mod = tmp_path / "mod"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    submod.mkdir()
    (submod / "a.txt").symlink_to(outside)
    (mod).mkdir()
    (mod / "a.txt").write_text("base", encoding="utf-8")

    assert validate_user_path("a.txt", [submod, mod]) == (mod / "a.txt").resolve()
