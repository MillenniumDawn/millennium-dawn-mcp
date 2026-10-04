"""Prefilter equivalence and stat-keyed text cache tests for find_references."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from md_mcp.analysis import text_cache
from md_mcp.analysis.refs import find_references
from md_mcp.analysis.text_cache import TextCache


@pytest.fixture(autouse=True)
def _fresh_cache():
    text_cache.clear()
    yield
    text_cache.clear()


def _bump_mtime(path: Path, delta_ns: int = 5_000_000_000) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_ns))


# --- prefilter equivalence -------------------------------------------------

_EXTRA = (
    "country_event = { id = Testns.1 }\n"
    "has_idea = TST_starter\n"
    "add_ideas = { idea = TST_starter }\n"
    "set_country_flag = TST_flag\n"
    "has_country_flag = { flag = TST_flag }\n"
    "set_variable = { var = TST_var value = 1 }\n"
    "tag = TST\n"
    "character = TST_test_character\n"
    "add_trait = TST_test_trait\n"
    "TST_effect = { log = yes }\n"
    "activate_decision = TST_decision\n"
    "focus = TST_root\n"
    "GFX_test_sprite_one\n"
    "# near miss: focus = TST_root_extra and TST_flagged\n"
)

_CASES = [
    ("focus", "TST_root"),
    ("focus", "TST_missing"),
    ("event", "Testns.1"),
    ("decision", "TST_decision"),
    ("idea", "TST_starter"),
    ("loc", "TST_root"),
    ("sprite", "GFX_test_sprite_one"),
    ("flag", "TST_flag"),
    ("variable", "TST_var"),
    ("country_tag", "TST"),
    ("tag", "TST"),
    ("character", "TST_test_character"),
    ("trait", "TST_test_trait"),
    ("scripted_effect", "TST_effect"),
    ("scripted_trigger", "TST_effect"),
]


@pytest.mark.parametrize("files_only", [False, True])
@pytest.mark.parametrize(("kind", "target"), _CASES)
def test_prefilter_matches_full_scan(fake_mod_root, kind, target, files_only):
    (fake_mod_root / "events" / "extra.txt").write_text(_EXTRA, encoding="utf-8")
    # BOM-prefixed file whose first line matches: BOM stripping must precede both paths.
    (fake_mod_root / "common" / "decisions" / "bom.txt").write_bytes(
        b"\xef\xbb\xbf" + b"focus = TST_root\ntag = TST\nhas_idea = TST_starter\n"
    )

    with_filter = find_references(
        fake_mod_root, kind, target, files_only=files_only, limit=1000, prefilter=True
    )
    text_cache.clear()
    without = find_references(
        fake_mod_root, kind, target, files_only=files_only, limit=1000, prefilter=False
    )

    assert with_filter == without
    assert with_filter["ok"]


def test_prefilter_equivalence_sanity_has_hits(fake_mod_root):
    (fake_mod_root / "events" / "extra.txt").write_text(_EXTRA, encoding="utf-8")
    r = find_references(fake_mod_root, "flag", "TST_flag", limit=1000)
    assert r["total"] == 2


def test_bom_stripped_once_so_columns_are_stable(fake_mod_root):
    p = fake_mod_root / "common" / "decisions" / "bom.txt"
    p.write_bytes(b"\xef\xbb\xbffocus = TST_bom_only\n")
    for _ in range(2):  # cold, then warm
        r = find_references(fake_mod_root, "focus", "TST_bom_only")
        assert r["total"] == 1
        assert (r["matches"][0]["line"], r["matches"][0]["col"]) == (1, 1)


# --- cache behaviour -------------------------------------------------------


def test_cache_hit_on_unchanged_file(tmp_path):
    cache = TextCache(max_bytes=1024)
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    assert cache.read(f) == "hello"
    assert (cache.hits, cache.misses) == (0, 1)
    assert cache.read(f) == "hello"
    assert (cache.hits, cache.misses) == (1, 1)


def test_cache_hit_does_not_reread_file(tmp_path, monkeypatch):
    cache = TextCache(max_bytes=1024)
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    cache.read(f)

    def boom(self):  # pragma: no cover - must not be called
        raise AssertionError("file re-read on cache hit")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert cache.read(f) == "hello"


def test_cache_miss_on_mtime_change(tmp_path):
    cache = TextCache(max_bytes=1024)
    f = tmp_path / "a.txt"
    f.write_text("aaaa", encoding="utf-8")
    cache.read(f)
    f.write_text("bbbb", encoding="utf-8")  # same size
    _bump_mtime(f)
    assert cache.read(f) == "bbbb"
    assert cache.misses == 2


def test_cache_miss_on_size_change(tmp_path):
    cache = TextCache(max_bytes=1024)
    f = tmp_path / "a.txt"
    f.write_text("aaaa", encoding="utf-8")
    st = f.stat()
    cache.read(f)
    f.write_text("bbbbbb", encoding="utf-8")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime; only size differs
    assert cache.read(f) == "bbbbbb"
    assert cache.misses == 2


def test_cache_evicts_lru_when_over_byte_bound(tmp_path):
    cache = TextCache(max_bytes=25)
    files = []
    for name in ("a", "b", "c"):
        f = tmp_path / f"{name}.txt"
        f.write_text(name * 10, encoding="utf-8")
        files.append(f)
    a, b, c = files
    cache.read(a)
    cache.read(b)
    cache.read(a)  # touch a -> b is now least recently used
    cache.read(c)  # 30 bytes > 25 -> evict b
    assert cache.total_bytes <= 25
    assert a in cache and c in cache and b not in cache
    misses = cache.misses
    cache.read(b)
    assert cache.misses == misses + 1


def test_cache_skips_file_larger_than_bound(tmp_path):
    cache = TextCache(max_bytes=5)
    f = tmp_path / "big.txt"
    f.write_text("x" * 50, encoding="utf-8")
    assert cache.read(f) == "x" * 50
    assert len(cache) == 0 and cache.total_bytes == 0


def test_cache_missing_file_returns_none_and_drops_entry(tmp_path):
    cache = TextCache(max_bytes=1024)
    f = tmp_path / "a.txt"
    f.write_text("hi", encoding="utf-8")
    cache.read(f)
    f.unlink()
    assert cache.read(f) is None
    assert len(cache) == 0 and cache.total_bytes == 0


def test_env_override_sets_bound(monkeypatch):
    monkeypatch.setenv(text_cache.ENV_VAR, "1234")
    assert TextCache().max_bytes == 1234
    monkeypatch.setenv(text_cache.ENV_VAR, "not-a-number")
    assert TextCache().max_bytes == text_cache.DEFAULT_MAX_BYTES
    monkeypatch.delenv(text_cache.ENV_VAR)
    assert TextCache().max_bytes == text_cache.DEFAULT_MAX_BYTES


def test_find_references_sees_edits_added_and_removed_files(fake_mod_root):
    assert find_references(fake_mod_root, "focus", "TST_new_ref")["total"] == 0
    f = fake_mod_root / "events" / "late.txt"
    f.write_text("focus = TST_new_ref\n", encoding="utf-8")  # added file
    assert find_references(fake_mod_root, "focus", "TST_new_ref")["total"] == 1
    f.write_text("focus = TST_new_ref\nfocus = TST_new_ref\n", encoding="utf-8")  # edited
    _bump_mtime(f)
    assert find_references(fake_mod_root, "focus", "TST_new_ref")["total"] == 2
    f.unlink()  # removed
    assert find_references(fake_mod_root, "focus", "TST_new_ref")["total"] == 0
