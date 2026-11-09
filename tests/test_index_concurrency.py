"""Deterministic concurrency regressions for the shared index refresh path (#328).

Interleavings are forced with events or barriers and asserted on state — no sleeps.
Covers one shared rebuild per cold index (including a caller paused across another's
completion), no partially applied refresh, unique writer temps over real cache JSON,
and temp cleanup with the previous data intact on a failed write.
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path

import pytest

from md_mcp.indexes.base import IndexCache
from tests.support.index_worker import make_index
from tests.support.indexes import write_stepped
from tests.support.mod import make_mod_root

# Bounds for gate release / join bookkeeping only; verdicts are state-based and
# BARRIER_TIMEOUT is just the solo-rebuild rendezvous fallback.
GATE_TIMEOUT = 10.0
BARRIER_TIMEOUT = 2.0


def _write_focus(root: Path, name: str, ids: list[str], step: int) -> Path:
    """Write one focus file (distinct mtime per step) and return its absolute path."""
    rel = Path("common/national_focus") / f"{name}.txt"
    text = (
        "focus_tree = {\n" + "".join(f" focus = {{ id = {key} x = 0 y = 0 }}\n" for key in ids)
    ) + "}\n"
    write_stepped(root / rel, text, step)
    return root / rel


def test_concurrent_cold_readers_share_one_rebuild(tmp_path, monkeypatch):
    """Two threads hitting a cold shared index parse its files exactly once."""
    root = make_mod_root(tmp_path / "Cold mod")
    cache = tmp_path / "Cold cache"
    _write_focus(root, "a", ["TST_a"], 1)
    _write_focus(root, "b", ["TST_b"], 2)
    index = make_index("focus", root, cache)

    # A second rebuild is the only way past this barrier; a shared rebuild parks
    # the second caller outside `_rebuild`, so the solo wait times out and proceeds.
    rendezvous = threading.Barrier(2, timeout=BARRIER_TIMEOUT)
    parse_calls: list[list[str]] = []
    real_parse = index._parse_parallel

    def gated_parse(relpaths: list[str]) -> list:
        parse_calls.append(list(relpaths))
        with contextlib.suppress(threading.BrokenBarrierError):
            rendezvous.wait()
        return real_parse(relpaths)

    monkeypatch.setattr(index, "_parse_parallel", gated_parse)

    results: dict[int, list] = {}
    errors: list[Exception] = []

    def load(slot: int) -> None:
        try:
            results[slot] = index.list_keys()
        except Exception as exc:  # pragma: no cover - surfaced by `errors` below
            errors.append(exc)

    threads = [threading.Thread(target=load, args=(slot,)) for slot in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(GATE_TIMEOUT)
        assert not thread.is_alive()

    assert errors == []
    assert len(parse_calls) == 1, f"expected one shared rebuild, saw parses {parse_calls}"
    assert results == {1: ["TST_a", "TST_b"], 2: ["TST_a", "TST_b"]}


def test_paused_cold_caller_shares_the_completed_rebuild(tmp_path, monkeypatch):
    """A cold caller paused across another's completed rebuild must not rebuild again."""
    root = make_mod_root(tmp_path / "Window mod")
    cache = tmp_path / "Window cache"
    _write_focus(root, "a", ["TST_a"], 1)
    index = make_index("focus", root, cache)

    rebuild_calls: list[str] = []
    real_rebuild = index._rebuild

    def counted_rebuild() -> None:
        rebuild_calls.append("rebuild")
        real_rebuild()

    monkeypatch.setattr(index, "_rebuild", counted_rebuild)

    # Gate one caller's first `_loaded` read: it observes the cold state, then
    # pauses before acting on it — the missed-completion window (#328).
    read_seen = threading.Event()
    release = threading.Event()
    seen: dict[str, object] = {}

    def gated_get(self):
        value = self.__dict__.get("_loaded", False)
        if not read_seen.is_set():
            seen["loaded"] = value
            read_seen.set()
            assert release.wait(GATE_TIMEOUT), "test harness never released the admission gate"
        return value

    def gated_set(self, value) -> None:
        self.__dict__["_loaded"] = value

    monkeypatch.setattr(type(index), "_loaded", property(gated_get, gated_set), raising=False)

    results: dict[str, list] = {}
    errors: list[Exception] = []

    def load(slot: str) -> None:
        try:
            results[slot] = index.list_keys()
        except Exception as exc:  # pragma: no cover - surfaced by `errors` below
            errors.append(exc)

    paused = threading.Thread(target=load, args=("paused",))
    paused.start()
    try:
        assert read_seen.wait(GATE_TIMEOUT), "paused caller never reached the admission read"
        load("completer")
    finally:
        release.set()
        paused.join(GATE_TIMEOUT)
    assert not paused.is_alive()

    assert seen["loaded"] is False
    assert errors == []
    assert len(rebuild_calls) == 1, f"paused caller rebuilt instead of sharing: {rebuild_calls}"
    assert results == {"paused": ["TST_a"], "completer": ["TST_a"]}


def test_readers_never_see_a_partially_applied_refresh(tmp_path, monkeypatch):
    """Readers during an in-flight refresh see one complete generation of every map."""
    root = make_mod_root(tmp_path / "Warm mod")
    cache = tmp_path / "Warm cache"
    _write_focus(root, "a", ["TST_keep"], 1)
    index = make_index("focus", root, cache)
    index.ensure_fresh()
    assert index.ids_for_tag("TST") == ["TST_keep"]

    _write_focus(root, "b", ["TST_new"], 2)
    index._stale_check.force_next()

    # Park the refresher between computing the new maps and publishing them: an
    # unsynchronised index exposes the new keys here beside the old tag maps.
    at_gate = threading.Event()
    release = threading.Event()
    real_tag_indexes = index._rebuild_tag_indexes

    def gated_tag_indexes(*args, **kwargs):
        at_gate.set()
        assert release.wait(GATE_TIMEOUT), "test harness never released the refresh"
        return real_tag_indexes(*args, **kwargs)

    monkeypatch.setattr(index, "_rebuild_tag_indexes", gated_tag_indexes)

    errors: list[Exception] = []

    def refresh() -> None:
        try:
            index.list_keys()
        except Exception as exc:  # pragma: no cover - surfaced by `errors` below
            errors.append(exc)

    refresher = threading.Thread(target=refresh)
    refresher.start()
    try:
        assert at_gate.wait(GATE_TIMEOUT), "refresh never reached the tag-index stage"

        observed: dict[str, object] = {}
        reader_done = threading.Event()

        def read_mid_refresh() -> None:
            observed["resolved"] = index.resolve("TST_new") is not None
            observed["in_tag_map"] = "TST_new" in index.ids_for_tag("TST")
            observed["keys"] = index.list_keys()
            reader_done.set()

        reader = threading.Thread(target=read_mid_refresh)
        reader.start()
        assert reader_done.wait(GATE_TIMEOUT), "readers blocked behind an in-flight refresh"
        reader.join(GATE_TIMEOUT)
        assert not reader.is_alive()

        assert (
            observed["resolved"] == observed["in_tag_map"]
        ), f"refresh partially visible to readers: {observed}"
        assert observed["keys"] in (["TST_keep"], ["TST_keep", "TST_new"])
    finally:
        release.set()
        refresher.join(GATE_TIMEOUT)
    assert not refresher.is_alive()
    assert errors == []

    # Once the refresh lands, every public view reports the new generation.
    assert index.resolve("TST_new") is not None
    assert "TST_new" in index.ids_for_tag("TST")
    assert index.list_keys() == ["TST_keep", "TST_new"]


def test_atomic_write_temp_names_are_unique_across_concurrent_writers(tmp_path, monkeypatch):
    """Concurrent cache writers never share a temp, and a reload reads one payload whole."""
    cache = IndexCache(tmp_path, "shared", 1)
    payloads = [{"files": {f"f{i}.txt": [{"id": f"K_{i}", "line": i}]}} for i in range(4)]
    written = threading.Barrier(len(payloads), timeout=GATE_TIMEOUT)
    temp_names: list[str] = []
    failures: list[Exception] = []
    real_replace = os.replace

    def recording_replace(src, dst, *args, **kwargs):
        temp_names.append(Path(src).name)
        written.wait()  # every writer has finished its temp before any rename
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", recording_replace)

    def write(payload: dict) -> None:
        try:
            cache.save_data(payload)
        except Exception as exc:  # pragma: no cover - surfaced by `failures` below
            failures.append(exc)

    threads = [threading.Thread(target=write, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(GATE_TIMEOUT)
        assert not thread.is_alive()

    assert len(set(temp_names)) == len(payloads), f"temp names collided: {temp_names}"
    assert failures == []
    assert IndexCache(tmp_path, "shared", 1).load_data() in payloads
    assert not list(tmp_path.rglob("*.tmp"))


def test_atomic_write_keeps_previous_data_and_removes_its_temp_on_replace_failure(
    tmp_path, monkeypatch
):
    """A failed replacement leaves the previous cache JSON readable and no temp behind."""
    cache = IndexCache(tmp_path, "shared", 1)
    previous = {"files": {"a.txt": [{"id": "K_a", "line": 1}]}}
    cache.save_data(previous)

    def failing_replace(src, dst, *args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated disk full"):
        cache.save_data({"files": {"b.txt": [{"id": "K_b", "line": 2}]}})

    assert cache.load_data() == previous
    assert not list(tmp_path.rglob("*.tmp"))
