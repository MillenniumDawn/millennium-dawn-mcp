"""Tests for the process-wide parsed-AST cache."""

from __future__ import annotations

import os
import threading

import pytest

from md_mcp.config import Settings
from md_mcp.indexes import FocusIndex
from md_mcp.paradox import ParseError, ast_cache
from md_mcp.paradox import parser as parser_mod
from md_mcp.tools.resolver_tools import resolve_focus_tool

SRC = "focus_tree = {\n    focus = { id = TST_a }\n}\n"


@pytest.fixture
def parse_counter(monkeypatch):
    calls = {"n": 0}
    real = parser_mod.parse_string

    def counting(text, error_prefix=""):
        calls["n"] += 1
        return real(text, error_prefix=error_prefix)

    monkeypatch.setattr(ast_cache, "parse_string", counting)
    return calls


def _write(path, text):
    # write_bytes: write_text would turn \n into \r\n on Windows and break the
    # text and byte-bound asserts.
    path.write_bytes(text.encode("utf-8"))
    return path


def test_hit_returns_same_objects_and_parses_once(tmp_path, parse_counter):
    f = _write(tmp_path / "a.txt", SRC)

    text1, root1 = ast_cache.parse_cached(f)
    text2, root2 = ast_cache.parse_cached(f)

    assert text1 == SRC
    assert text2 is text1
    assert root2 is root1
    assert parse_counter["n"] == 1


def test_miss_on_mtime_change(tmp_path, parse_counter):
    f = _write(tmp_path / "a.txt", SRC)
    ast_cache.parse_cached(f)
    st = os.stat(f)

    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    ast_cache.parse_cached(f)

    assert parse_counter["n"] == 2


def test_miss_on_size_change(tmp_path, parse_counter):
    f = _write(tmp_path / "a.txt", SRC)
    _, root1 = ast_cache.parse_cached(f)
    st = os.stat(f)

    _write(f, SRC + "# longer\n")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime, different size
    text, root2 = ast_cache.parse_cached(f)

    assert text.endswith("# longer\n")
    assert root2 is not root1
    assert parse_counter["n"] == 2


def test_lru_eviction(tmp_path, monkeypatch, parse_counter):
    monkeypatch.setenv("MD_MCP_AST_CACHE_SIZE", "2")
    a, b, c = (_write(tmp_path / f"{n}.txt", SRC) for n in "abc")

    ast_cache.parse_cached(a)
    ast_cache.parse_cached(b)
    ast_cache.parse_cached(a)  # a is now most recent; b is the LRU entry
    ast_cache.parse_cached(c)  # evicts b
    assert ast_cache.size() == 2
    assert parse_counter["n"] == 3

    ast_cache.parse_cached(a)  # still cached
    assert parse_counter["n"] == 3
    ast_cache.parse_cached(b)  # evicted, re-parsed
    assert parse_counter["n"] == 4


def test_cache_disabled_with_size_zero(tmp_path, monkeypatch, parse_counter):
    monkeypatch.setenv("MD_MCP_AST_CACHE_SIZE", "0")
    f = _write(tmp_path / "a.txt", SRC)

    ast_cache.parse_cached(f)
    ast_cache.parse_cached(f)

    assert parse_counter["n"] == 2
    assert ast_cache.size() == 0


def test_invalid_size_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MD_MCP_AST_CACHE_SIZE", "lots")
    assert ast_cache.max_entries() == ast_cache.DEFAULT_CACHE_SIZE
    monkeypatch.setenv("MD_MCP_AST_CACHE_BYTES", "many")
    assert ast_cache.max_bytes() == ast_cache.DEFAULT_CACHE_BYTES


def test_byte_bound_evicts_oldest(tmp_path, monkeypatch, parse_counter):
    # Room for two copies of SRC, not three.
    monkeypatch.setenv("MD_MCP_AST_CACHE_BYTES", str(2 * len(SRC)))
    a, b, c = (_write(tmp_path / f"{n}.txt", SRC) for n in "abc")

    ast_cache.parse_cached(a)
    ast_cache.parse_cached(b)
    assert ast_cache.size() == 2
    assert ast_cache.total_chars() == 2 * len(SRC)

    ast_cache.parse_cached(c)  # evicts a
    assert ast_cache.size() == 2
    assert ast_cache.total_chars() == 2 * len(SRC)
    ast_cache.parse_cached(b)  # still cached
    assert parse_counter["n"] == 3
    ast_cache.parse_cached(a)  # evicted, re-parsed
    assert parse_counter["n"] == 4


def test_edit_replaces_the_stale_entry_for_the_same_path(tmp_path, monkeypatch, parse_counter):
    monkeypatch.setenv("MD_MCP_AST_CACHE_BYTES", str(10 * len(SRC)))
    f = _write(tmp_path / "a.txt", SRC)
    for n in range(5):
        _write(f, SRC + f"# edit {n}\n")
        st = os.stat(f)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + (n + 1) * 1_000_000_000))
        ast_cache.parse_cached(f)
        assert ast_cache.size() == 1
        assert ast_cache.total_chars() == len(SRC + f"# edit {n}\n")
    assert parse_counter["n"] == 5


def test_file_larger_than_byte_bound_is_not_cached(tmp_path, monkeypatch, parse_counter):
    monkeypatch.setenv("MD_MCP_AST_CACHE_BYTES", str(len(SRC) - 1))
    f = _write(tmp_path / "a.txt", SRC)

    ast_cache.parse_cached(f)
    ast_cache.parse_cached(f)

    assert parse_counter["n"] == 2
    assert ast_cache.size() == 0
    assert ast_cache.total_chars() == 0


def test_parse_error_not_cached(tmp_path, parse_counter):
    f = _write(tmp_path / "bad.txt", "focus_tree = { focus = { id = x ")

    with pytest.raises(ParseError):
        ast_cache.parse_cached(f)
    with pytest.raises(ParseError):
        ast_cache.parse_cached(f, error_prefix="In file bad.txt:\n")

    assert ast_cache.size() == 0
    assert parse_counter["n"] == 2


def test_error_prefix_applies_to_errors_only(tmp_path):
    f = _write(tmp_path / "bad.txt", "focus_tree = { focus = { id = x ")

    with pytest.raises(ParseError, match="In file bad.txt:"):
        ast_cache.parse_cached(f, error_prefix="In file bad.txt:\n")


def test_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        ast_cache.parse_cached(tmp_path / "nope.txt")
    assert ast_cache.size() == 0


def test_clear_drops_entries(tmp_path, parse_counter):
    f = _write(tmp_path / "a.txt", SRC)
    ast_cache.parse_cached(f)
    ast_cache.clear()
    assert ast_cache.size() == 0
    ast_cache.parse_cached(f)
    assert parse_counter["n"] == 2


def test_thread_safe_under_concurrent_access(tmp_path, monkeypatch):
    monkeypatch.setenv("MD_MCP_AST_CACHE_SIZE", "3")
    files = [_write(tmp_path / f"{i}.txt", SRC) for i in range(8)]
    errors: list[BaseException] = []

    def work():
        try:
            for _ in range(20):
                for f in files:
                    text, _root = ast_cache.parse_cached(f)
                    assert text == SRC
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert ast_cache.size() <= 3


def test_resolve_focus_tool_twice_parses_once(fake_mod_root, tmp_path, parse_counter):
    settings = Settings(mod_root=fake_mod_root, vanilla_path=None, cache_dir=tmp_path / "cache")
    index = FocusIndex(fake_mod_root, settings.cache_dir)
    index.resolve("TST_root")  # build the index; its parses are not the cache's
    parse_counter["n"] = 0

    first = resolve_focus_tool("TST_root", settings, index)
    second = resolve_focus_tool("TST_root", settings, index)

    assert first == second
    assert first["ok"] is True
    assert "warning" not in first
    assert parse_counter["n"] == 1
