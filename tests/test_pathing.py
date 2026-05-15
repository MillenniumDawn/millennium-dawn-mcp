"""Discovery + config tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from md_mcp.util.pathing import ModRootNotFound, find_mod_root, find_vanilla_path


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
