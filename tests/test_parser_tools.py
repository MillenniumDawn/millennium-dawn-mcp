"""Tests for the parser MCP tools: max_bytes guard, top_level_only mode, path guards."""

from __future__ import annotations

import pytest

from md_mcp.tools import parser_tools
from md_mcp.tools.parser_tools import parse_file_tool, parse_string_tool

SCRIPT = "focus = { id = X cost = 1 }"


def test_parse_string_returns_the_ast():
    out = parse_string_tool("a = 1")
    assert out["ok"] is True
    assert out["root"]["value"]["children"][0]["name"] == "a"


def test_parse_string_reports_the_error_position():
    out = parse_string_tool("a = 1\nb = {{{")
    assert out["ok"] is False
    assert "at (2, 6)" in out["error"]


def test_parse_file_refuses_oversized(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("a = 1\n" * 1000)  # ~6000 bytes
    out = parse_file_tool(str(big), tmp_path, max_bytes=100)
    assert out["ok"] is False
    assert "max_bytes" in out["error"]
    assert out["size"] >= 100


def test_parse_file_normal(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text(SCRIPT)
    out = parse_file_tool(str(f), tmp_path)
    assert out["ok"] is True
    assert "root" in out


def test_parse_file_top_level_only_lists_names_and_lines(tmp_path):
    f = tmp_path / "top.txt"
    f.write_text("focus_tree = { id = T }\nshared_focus = { id = S }\n\nlast = 1\n")
    out = parse_file_tool(str(f), tmp_path, top_level_only=True)
    assert out["ok"] is True
    assert "root" not in out
    assert out["top_level"] == [
        {"name": "focus_tree", "line": 1},
        {"name": "shared_focus", "line": 2},
        {"name": "last", "line": 4},
    ]


def test_parse_file_reports_a_parse_error_with_the_path(tmp_path):
    f = tmp_path / "broken.txt"
    f.write_text("a = {{{")
    out = parse_file_tool(str(f), tmp_path)
    assert out["ok"] is False
    assert out["path"] == str(f.resolve())
    assert f"In file {f.resolve()}" in out["error"]


def test_parse_file_reports_a_file_that_vanished_after_the_path_check(tmp_path, monkeypatch):
    monkeypatch.setattr(parser_tools, "_resolve_path", lambda *args: tmp_path / "gone.txt")
    out = parse_file_tool("gone.txt", tmp_path)
    assert out["ok"] is False
    assert "Could not stat" in out["error"]


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
    (mod_root / "focus.txt").write_text(SCRIPT)
    out = parse_file_tool("focus.txt", mod_root)
    assert out["ok"] is True


def test_parse_file_allows_absolute_path_under_mod_root(mod_root):
    f = mod_root / "focus.txt"
    f.write_text(SCRIPT)
    out = parse_file_tool(str(f), mod_root)
    assert out["ok"] is True


def test_parse_file_allows_absolute_path_under_vanilla_path(mod_root, vanilla_path):
    f = vanilla_path / "focus.txt"
    f.write_text(SCRIPT)
    out = parse_file_tool(str(f), mod_root, vanilla_path)
    assert out["ok"] is True


def test_parse_file_rejects_path_outside_both_roots(mod_root, vanilla_path, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(SCRIPT)
    out = parse_file_tool(str(outside), mod_root, vanilla_path)
    assert out["ok"] is False
    assert str(mod_root) in out["error"]
    assert str(vanilla_path) in out["error"]


@pytest.mark.parametrize(
    ("path", "with_submod"),
    [
        pytest.param("../outside.txt", False, id="parent-traversal"),
        pytest.param("../outside.txt", True, id="parent-traversal-with-submod"),
        pytest.param("link.txt", False, id="symlink-escape"),
        pytest.param("subdir", False, id="directory"),
        pytest.param("", False, id="empty"),
        pytest.param(".", False, id="dot"),
        pytest.param("./", False, id="dot-slash"),
    ],
)
def test_parse_file_rejects_paths_that_escape_or_are_not_files(
    mod_root, tmp_path, path, with_submod
):
    outside = tmp_path / "outside.txt"
    outside.write_text(SCRIPT)
    (mod_root / "link.txt").symlink_to(outside)
    (mod_root / "subdir").mkdir()
    submod = tmp_path / "submod"
    submod.mkdir()

    out = parse_file_tool(path, mod_root, submod_root=submod if with_submod else None)

    assert out["ok"] is False


def test_parse_file_rejects_unsupported_extension(mod_root):
    (mod_root / "notes.md").write_text("not paradox script")
    out = parse_file_tool("notes.md", mod_root)
    assert out["ok"] is False
    assert "extension" in out["error"]


def test_parse_file_relative_falls_back_to_base_and_vanilla_with_submod(
    mod_root, vanilla_path, tmp_path
):
    submod = tmp_path / "submod"
    submod.mkdir()
    (mod_root / "base_only.txt").write_text("focus = { id = B cost = 1 }")
    (vanilla_path / "vanilla_only.txt").write_text("focus = { id = V cost = 1 }")
    (mod_root / "shared.txt").write_text("focus = { id = BASE cost = 1 }")
    (submod / "shared.txt").write_text("focus = { id = OVERLAY cost = 1 }")

    base = parse_file_tool("base_only.txt", mod_root, vanilla_path, submod_root=submod)
    vanilla = parse_file_tool("vanilla_only.txt", mod_root, vanilla_path, submod_root=submod)
    shared = parse_file_tool("shared.txt", mod_root, vanilla_path, submod_root=submod)

    assert base["path"] == str((mod_root / "base_only.txt").resolve())
    assert vanilla["path"] == str((vanilla_path / "vanilla_only.txt").resolve())
    assert shared["path"] == str((submod / "shared.txt").resolve())
