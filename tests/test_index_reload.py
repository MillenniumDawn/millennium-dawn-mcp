"""Warm index edits and persistence parity across real headless process reloads."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from md_mcp.indexes import base as base_mod
from md_mcp.util.process import run_in_group
from tests.support.index_worker import make_index, snapshot
from tests.support.indexes import force_refresh, spy_parse, write_stepped
from tests.support.mod import make_mod_root

REPO = Path(__file__).resolve().parents[1]


def _write(root, kind, name, entries, step):
    if kind == "focus":
        rel = Path("common/national_focus") / f"{name}.txt"
        text = (
            "focus_tree = {\n"
            + "".join(f" focus = {{ id = {key} x = {value} y = 0 }}\n" for key, value in entries)
            + "}\n"
        )
    else:
        rel = Path("localisation/english") / f"{name}_l_english.yml"
        text = "\ufeffl_english:\n" + "".join(f' {key}:0 "{value}"\n' for key, value in entries)
    write_stepped(root / rel, text, step)
    return rel


def _reload(kind, root, cache, mode="serial"):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(REPO), str(REPO / "src")))
    result = run_in_group(
        [sys.executable, "-m", "tests.support.index_worker", kind, str(root), str(cache), mode],
        cwd=str(root),
        env=env,
        timeout=30,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pid"] != os.getpid()
    assert payload["stdin"] == ""
    return payload


@pytest.mark.parametrize("kind", ["focus", "loc"])
def test_warm_edits_and_real_reload_match_scratch_with_parse_and_write_counts(
    tmp_path, monkeypatch, kind
):
    root = make_mod_root(tmp_path / "Mod space 界")
    cache = tmp_path / "Cache space 界"
    a = _write(root, kind, "a", [("TST_shared", 1), ("TST_a", 2)], 1)
    b = _write(root, kind, "b", [("TST_shared", 3), ("TST_b", 4)], 2)
    _write(root, kind, "c", [("TST_c", 5)], 3)
    if kind == "focus":
        write_stepped(root / "common/national_focus/broken.txt", "focus_tree = {", 4)
    index = make_index(kind, root, cache)
    index.ensure_fresh()
    parsed = spy_parse(index, monkeypatch)
    writes = []
    real_write = base_mod._atomic_write_text

    def write(path, text):
        writes.append(path)
        return real_write(path, text)

    monkeypatch.setattr(base_mod, "_atomic_write_text", write)
    untouched = index._by_key["TST_c" if kind == "focus" else ("l_english", "TST_c")]

    def check(expected_parses, expected_writes, step):
        assert [rel for batch in parsed for rel in batch] == expected_parses
        assert writes == expected_writes
        reloaded = _reload(kind, root, cache)
        assert reloaded["parsed"] == []
        assert reloaded["writes"] == []
        warm = snapshot(index)
        assert reloaded["state"] == warm
        assert snapshot(make_index(kind, root, tmp_path / f"scratch-{step}")) == warm
        parsed.clear()
        writes.clear()

    force_refresh(index)
    check([], [], "unchanged")
    _write(root, kind, "b", [("TST_b", 6)], 10)
    force_refresh(index)
    data = index._cache.data_path if kind == "focus" else index._cache.shard_path(str(b))
    check([str(b)], [data, index._cache.manifest_path], "edit")
    assert index._by_key["TST_c" if kind == "focus" else ("l_english", "TST_c")] is untouched
    assert index.duplicates() == {}
    shared = index._by_key["TST_shared" if kind == "focus" else ("l_english", "TST_shared")]
    assert shared["file"] == str(a)

    z = _write(root, kind, "z", [("TST_shared", 7)], 11)
    force_refresh(index)
    data = index._cache.data_path if kind == "focus" else index._cache.shard_path(str(z))
    check([str(z)], [data, index._cache.manifest_path], "add")
    assert list(index.duplicates().values()) == [[str(a)]]
    (root / z).unlink()
    force_refresh(index)
    expected = (
        [index._cache.data_path, index._cache.manifest_path]
        if kind == "focus"
        else [index._cache.manifest_path]
    )
    check([], expected, "remove")
    if kind == "loc":
        assert not index._cache.shard_path(str(z)).exists()


@pytest.mark.parametrize("kind", ["focus", "loc"])
def test_real_spawn_pool_matches_serial_and_reloads_without_work(tmp_path, kind):
    root = make_mod_root(tmp_path / "Spawn mod space 界")
    for number in range(5):
        _write(root, kind, f"file{number}", [(f"TST_{number}", number)], number)
    if kind == "focus":
        write_stepped(root / "common/national_focus/broken.txt", "focus_tree = {", 8)
    cache = tmp_path / "Spawn cache space 界"
    pooled = _reload(kind, root, cache, "spawn")
    assert pooled["contexts"] == ["spawn"]
    assert len(pooled["parsed"]) == (6 if kind == "focus" else 5)
    assert len(pooled["writes"]) == (2 if kind == "focus" else 6)
    assert pooled["state"] == snapshot(make_index(kind, root, tmp_path / "serial"))
    reloaded = _reload(kind, root, cache, "spawn")
    assert reloaded["contexts"] == []
    assert reloaded["parsed"] == reloaded["writes"] == []
    assert reloaded["state"] == pooled["state"]


@pytest.mark.parametrize("kind", ["focus", "loc"])
@pytest.mark.parametrize("change", ["edit", "add", "remove"])
def test_external_process_manifest_does_not_hide_changes_from_warm_index(
    tmp_path, monkeypatch, kind, change
):
    root = make_mod_root(tmp_path / "Shared mod")
    cache = tmp_path / "Shared cache"
    a = _write(root, kind, "a", [("TST_shared", 1)], 1)
    b = _write(root, kind, "b", [("TST_shared", 2), ("TST_b", 3)], 2)
    index = make_index(kind, root, cache)
    index.ensure_fresh()
    if change == "edit":
        _write(root, kind, "b", [("TST_b", 4)], 10)
        expected_parsed = [str(b)]
    elif change == "add":
        z = _write(root, kind, "z", [("TST_shared", 5)], 10)
        expected_parsed = [str(z)]
    else:
        (root / b).unlink()
        expected_parsed = []

    external = _reload(kind, root, cache)
    assert external["parsed"] == expected_parsed
    data = (
        index._cache.data_path
        if kind == "focus"
        else index._cache.shard_path(expected_parsed[0])
        if expected_parsed
        else None
    )
    expected_writes = [data, index._cache.manifest_path] if data else [index._cache.manifest_path]
    assert external["writes"] == [str(path.relative_to(cache)) for path in expected_writes]
    expected_state = snapshot(make_index(kind, root, tmp_path / "scratch"))
    assert external["state"] == expected_state
    parsed = spy_parse(index, monkeypatch)
    writes = []
    write = base_mod._atomic_write_text

    def counted_write(path, text):
        writes.append(path)
        return write(path, text)

    monkeypatch.setattr(base_mod, "_atomic_write_text", counted_write)
    force_refresh(index)
    assert [rel for batch in parsed for rel in batch] == expected_parsed
    assert writes == expected_writes
    assert snapshot(index) == expected_state
    if change == "remove":
        key = "TST_shared" if kind == "focus" else ("l_english", "TST_shared")
        assert index._by_key[key]["file"] == str(a)


def test_build_index_cli_is_headless_with_native_pool_and_unicode_paths(tmp_path):
    root = make_mod_root(tmp_path / "CLI mod space 界")
    cache = tmp_path / "CLI cache space 界"
    for number in range(5):
        _write(root, "focus", f"file{number}", [(f"TST_{number}", number)], number)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MD_") and key != "HOI4_PATH"
    }
    env["MD_MCP_CACHE_DIR"] = str(cache)
    env["PYTHONPATH"] = str(REPO / "src")
    code = (
        "from pathlib import Path; from md_mcp import config; "
        "config.CONFIG_PATH = Path('absent-test-config.toml'); "
        "from md_mcp.server import main; main()"
    )
    result = run_in_group(
        [sys.executable, "-c", code, "build-index", "--mod-root", str(root)],
        cwd=str(root),
        env=env,
        timeout=30,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    focus_line = next(line for line in result.stdout.splitlines() if line.startswith("FocusIndex"))
    assert focus_line.split() == ["FocusIndex", "5", "keys", "5", "files"]
    assert _reload("focus", root, cache)["parsed"] == []
