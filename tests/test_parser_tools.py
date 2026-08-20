"""Tests for the parser MCP tools — max_bytes guard, top_level_only mode, path guards."""

from __future__ import annotations

import pytest

from md_mcp.tools.parser_tools import parse_file_tool


def test_parse_file_refuses_oversized(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("a = 1\n" * 1000)  # ~6000 bytes
    out = parse_file_tool(str(big), tmp_path, max_bytes=100)
    assert out["ok"] is False
    assert "max_bytes" in out["error"]
    assert out["size"] >= 100


def test_parse_file_normal(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("focus = { id = X cost = 1 }")
    out = parse_file_tool(str(f), tmp_path)
    assert out["ok"] is True
    assert "root" in out


def test_parse_file_top_level_only(tmp_path):
    f = tmp_path / "two_top.txt"
    f.write_text("focus_tree = { id = T }\nshared_focus = { id = S }")
    out = parse_file_tool(str(f), tmp_path, top_level_only=True)
    assert out["ok"] is True
    assert "top_level" in out
    assert "root" not in out
    names = [n["name"] for n in out["top_level"]]
    # We at least get two entries; exact name handling depends on Node shape.
    assert len(names) == 2


@pytest.fixture
def mod_root(tmp_path):
    mod = tmp_path / "mod"
    mod.mkdir()
    return mod


@pytest.fixture
def vanilla_path(tmp_path):
    vanilla = tmp_path / "vanilla"
    vanilla.mkdir()
    return vanilla


def test_parse_file_allows_mod_relative_path(mod_root):
    (mod_root / "focus.txt").write_text("focus = { id = X cost = 1 }")
    out = parse_file_tool("focus.txt", mod_root)
    assert out["ok"] is True


def test_parse_file_allows_absolute_path_under_mod_root(mod_root):
    f = mod_root / "focus.txt"
    f.write_text("focus = { id = X cost = 1 }")
    out = parse_file_tool(str(f), mod_root)
    assert out["ok"] is True


def test_parse_file_allows_absolute_path_under_vanilla_path(mod_root, vanilla_path):
    f = vanilla_path / "focus.txt"
    f.write_text("focus = { id = X cost = 1 }")
    out = parse_file_tool(str(f), mod_root, vanilla_path)
    assert out["ok"] is True


def test_parse_file_rejects_path_outside_both_roots(mod_root, vanilla_path, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("focus = { id = X cost = 1 }")
    out = parse_file_tool(str(outside), mod_root, vanilla_path)
    assert out["ok"] is False
    assert str(mod_root) in out["error"]
    assert str(vanilla_path) in out["error"]


def test_parse_file_rejects_parent_traversal(mod_root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("focus = { id = X cost = 1 }")
    out = parse_file_tool("../outside.txt", mod_root)
    assert out["ok"] is False


def test_parse_file_rejects_symlink_escape(mod_root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("focus = { id = X cost = 1 }")
    (mod_root / "link.txt").symlink_to(outside)
    out = parse_file_tool("link.txt", mod_root)
    assert out["ok"] is False


def test_parse_file_rejects_directory(mod_root):
    (mod_root / "subdir").mkdir()
    out = parse_file_tool("subdir", mod_root)
    assert out["ok"] is False


def test_parse_file_rejects_unsupported_extension(mod_root):
    (mod_root / "notes.md").write_text("not paradox script")
    out = parse_file_tool("notes.md", mod_root)
    assert out["ok"] is False
    assert "extension" in out["error"]
