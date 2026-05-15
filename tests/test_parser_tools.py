"""Tests for the parser MCP tools — max_bytes guard, top_level_only mode."""

from __future__ import annotations

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
