"""Cache recovery: monolithic `data.json` damage must reparse, not silently drop (issue #152).

Counterpart to `tests/test_index_sharding.py`, which already proves a corrupt shard
reparses only that file. Here the monolithic payload is damaged behind an intact
manifest, and the index must reparse just the unusable entries, keep healthy ones
as cache hits, and persist the repair so the next start is a clean cache hit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from md_mcp.indexes import FocusIndex, GenericTxtIndex, IdeaIndex, LocalisationIndex
from tests.test_index_sharding import _spy_parse, _state

_IDEA_DIR = Path("common") / "ideas"
_FOCUS_DIR = Path("common") / "national_focus"
_LOC_DIR = Path("localisation") / "english"


def _idea(*ids: str) -> str:
    body = "".join(f"\t\t{i} = {{ picture = generic_idea }}\n" for i in ids)
    return f"ideas = {{\n\tcountry = {{\n{body}\t}}\n}}\n"


def _write_file(path: Path, text: str, step: int) -> None:
    """Write `path` and give it a distinct mtime so the signature always moves."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    ns = 1_700_000_000_000_000_000 + step * 1_000_000_000
    os.utime(path, ns=(ns, ns))


def _mk(root: Path, cache: Path) -> IdeaIndex:
    return IdeaIndex(root, cache, include_vanilla=False)


def _scratch_state(cls: Any, root: Path, tmp_path: Path, tag: str) -> tuple:
    """State of a brand-new instance with an empty cache: the ground truth."""
    idx = cls(root, tmp_path / f"scratch-{tag}", include_vanilla=False)
    idx.ensure_fresh()
    return _state(idx)


def _drop_data(idx: GenericTxtIndex) -> None:
    idx._cache.data_path.unlink()


def _overwrite_data(idx: GenericTxtIndex, text: str) -> None:
    idx._cache.data_path.write_text(text, encoding="utf-8")


def _damage_entry(idx: GenericTxtIndex, relpath: str, bad_records: Any) -> None:
    """Replace one file's records in the persisted payload, keeping the rest authentic."""
    payload = idx._cache.load_data()
    assert isinstance(payload, dict) and isinstance(payload.get("files"), dict)
    assert relpath in payload["files"]
    payload["files"][relpath] = bad_records
    idx._cache.save_data(payload)


def _rel(subdir: Path, name: str) -> str:
    return str(subdir / f"{name}.txt")


@pytest.fixture
def idea_root(tmp_path: Path) -> Path:
    root = tmp_path / "IdeaMod"
    _write_file(root / _IDEA_DIR / "a.txt", _idea("TST_a"), 1)
    return root


@pytest.fixture
def two_idea_root(tmp_path: Path) -> Path:
    root = tmp_path / "IdeaMod2"
    _write_file(root / _IDEA_DIR / "a.txt", _idea("TST_a"), 1)
    _write_file(root / _IDEA_DIR / "b.txt", _idea("TST_b", "TST_shared"), 2)
    return root


# ----- payload damage behind an intact manifest -------------------------------


def test_missing_data_file_reparses_and_heals(idea_root, cache_dir, tmp_path, monkeypatch):
    first = _mk(idea_root, cache_dir)
    first.ensure_fresh()
    _drop_data(first)

    second = _mk(idea_root, cache_dir)
    parsed = _spy_parse(second, monkeypatch)
    second.ensure_fresh()
    assert parsed == [[_rel(_IDEA_DIR, "a")]]
    assert _state(second) == _scratch_state(IdeaIndex, idea_root, tmp_path, "missing")

    # The repair was persisted: a third process parses nothing and matches.
    third = _mk(idea_root, cache_dir)
    parsed = _spy_parse(third, monkeypatch)
    third.ensure_fresh()
    assert parsed == []
    assert _state(third) == _state(second)


@pytest.mark.parametrize(
    "garbage",
    [
        "not json at all",
        "[]",
        "[1, 2, 3]",
        "5",
        '"just a string"',
        '{"files": []}',
        '{"files": 5}',
        '{"unrelated": true}',
        json.dumps({"files": {_rel(_IDEA_DIR, "a"): None}}),
        json.dumps({"files": {_rel(_IDEA_DIR, "a"): {"id": "TST_a"}}}),
        json.dumps({"files": {_rel(_IDEA_DIR, "a"): [1, "str", None]}}),
        json.dumps({"files": {_rel(_IDEA_DIR, "a"): [{"id": ["unhashable"]}]}}),
        json.dumps({"files": {_rel(_IDEA_DIR, "a"): [{"id": {"nested": 1}}]}}),
        json.dumps({"files": {_rel(_IDEA_DIR, "a"): [{}]}}),
        json.dumps({"files": {_rel(_IDEA_DIR, "a"): [{"id": None}]}}),
    ],
)
def test_damaged_data_reparses_instead_of_dropping(
    idea_root, cache_dir, tmp_path, monkeypatch, garbage
):
    first = _mk(idea_root, cache_dir)
    first.ensure_fresh()
    ids = sorted(first.list_keys())
    assert ids
    _overwrite_data(first, garbage)

    second = _mk(idea_root, cache_dir)
    parsed = _spy_parse(second, monkeypatch)
    second.ensure_fresh()
    assert parsed == [[_rel(_IDEA_DIR, "a")]]
    assert sorted(second.list_keys()) == ids
    assert _state(second) == _scratch_state(IdeaIndex, idea_root, tmp_path, "damaged")

    third = _mk(idea_root, cache_dir)
    parsed = _spy_parse(third, monkeypatch)
    third.ensure_fresh()
    assert parsed == []
    assert _state(third) == _state(second)


def test_invalid_utf8_data_reparses_and_heals(idea_root, cache_dir, monkeypatch):
    first = _mk(idea_root, cache_dir)
    first.ensure_fresh()
    first._cache.data_path.write_bytes(b"\xff")

    second = _mk(idea_root, cache_dir)
    parsed = _spy_parse(second, monkeypatch)
    assert second.resolve("TST_a") is not None
    assert parsed == [[_rel(_IDEA_DIR, "a")]]

    third = _mk(idea_root, cache_dir)
    parsed = _spy_parse(third, monkeypatch)
    assert third.resolve("TST_a") is not None
    assert parsed == []


def test_mixed_healthy_and_damaged_reparses_only_damaged(
    two_idea_root, cache_dir, tmp_path, monkeypatch
):
    first = _mk(two_idea_root, cache_dir)
    first.ensure_fresh()
    a, b = _rel(_IDEA_DIR, "a"), _rel(_IDEA_DIR, "b")
    _damage_entry(first, b, "garbage")

    second = _mk(two_idea_root, cache_dir)
    parsed = _spy_parse(second, monkeypatch)
    second.ensure_fresh()
    assert parsed == [[b]]
    rec_a = second.resolve("TST_a")
    assert rec_a is not None
    assert rec_a["file"] == a
    assert _state(second) == _scratch_state(IdeaIndex, two_idea_root, tmp_path, "mixed")

    third = _mk(two_idea_root, cache_dir)
    parsed = _spy_parse(third, monkeypatch)
    third.ensure_fresh()
    assert parsed == []
    assert _state(third) == _state(second)
    # Last-write-wins against the healthy cached file is preserved.
    assert sorted(third.duplicates()) == sorted(second.duplicates())


def test_repair_matches_cold_build_last_write_wins(two_idea_root, tmp_path):
    """The repaired winner/duplicate state must match a cold build."""
    root = tmp_path / "LWWMod"
    _write_file(root / _IDEA_DIR / "a.txt", _idea("TST_d", "TST_only_a"), 1)
    _write_file(root / _IDEA_DIR / "b.txt", _idea("TST_d"), 2)
    cache = tmp_path / "cache-lww"
    first = _mk(root, cache)
    first.ensure_fresh()
    a, b = _rel(_IDEA_DIR, "a"), _rel(_IDEA_DIR, "b")
    # Damage b's records behind an intact manifest; a stays a cache hit.
    _damage_entry(first, b, "garbage")

    second = _mk(root, cache)
    second.ensure_fresh()
    rec_d = second.resolve("TST_d")
    assert rec_d is not None
    assert rec_d["file"] == b
    assert second.duplicates() == {"TST_d": [a]}
    assert second.resolve("TST_only_a") is not None
    assert _state(second) == _scratch_state(IdeaIndex, root, tmp_path, "lww")


def test_valid_empty_and_healthy_files_stay_cache_hits(two_idea_root, cache_dir, monkeypatch):
    """A file that parses to zero records must not be flagged damaged."""
    first = _mk(two_idea_root, cache_dir)
    first.ensure_fresh()
    a, b = _rel(_IDEA_DIR, "a"), _rel(_IDEA_DIR, "b")
    _overwrite_data(
        first,
        json.dumps({"files": {a: [], b: [{"id": "TST_b"}, {"id": "TST_shared"}]}}),
    )

    second = _mk(two_idea_root, cache_dir)
    parsed = _spy_parse(second, monkeypatch)
    second.ensure_fresh()
    assert parsed == []
    assert second.records_for_file(a) == []
    assert second.list_keys() == ["TST_b", "TST_shared"]


def test_repeated_queries_recover_without_source_edits(idea_root, cache_dir):
    first = _mk(idea_root, cache_dir)
    first.ensure_fresh()
    _drop_data(first)

    second = _mk(idea_root, cache_dir)
    for _ in range(3):
        second._stale_check.force_next()
        second.ensure_fresh()
        assert second.resolve("TST_a") is not None
        assert second.list_keys() == ["TST_a"]
        assert second.list_files() == [_rel(_IDEA_DIR, "a")]


# ----- parse-error contract ----------------------------------------------------


def test_cached_parse_errors_not_reparsed_after_restart(tmp_path, monkeypatch):
    root = tmp_path / "FocusMod"
    _write_file(
        root / _FOCUS_DIR / "good.txt",
        "focus_tree = { focus = { id = TST_good x = 1 y = 0 } }",
        1,
    )
    _write_file(root / _FOCUS_DIR / "bad.txt", "focus_tree = { focus = { id = TST_bad x = {{{", 2)
    cache = tmp_path / "cache"

    first = FocusIndex(root, cache, include_vanilla=False)
    first.ensure_fresh()
    expected_errors = first.parse_errors()
    assert [e["file"] for e in expected_errors] == [_rel(_FOCUS_DIR, "bad")]

    second = FocusIndex(root, cache, include_vanilla=False)
    parsed = _spy_parse(second, monkeypatch)
    assert second.parse_errors() == expected_errors
    assert second.resolve("TST_good") is not None
    assert parsed == []  # neither the good nor the known-bad file was reparsed


def test_damaged_payload_does_not_lose_parse_errors(tmp_path, monkeypatch):
    root = tmp_path / "FocusMod2"
    _write_file(
        root / _FOCUS_DIR / "good.txt",
        "focus_tree = { focus = { id = TST_good x = 1 y = 0 } }",
        1,
    )
    _write_file(root / _FOCUS_DIR / "bad.txt", "focus_tree = { focus = { id = TST_bad x = {{{", 2)
    cache = tmp_path / "cache"

    first = FocusIndex(root, cache, include_vanilla=False)
    first.ensure_fresh()
    expected_errors = first.parse_errors()
    assert [e["file"] for e in expected_errors] == [_rel(_FOCUS_DIR, "bad")]
    # Records lost, but the parse-error entries stay usable.
    _overwrite_data(
        first,
        json.dumps({"files": {}, "parse_errors": {e["file"]: e["error"] for e in expected_errors}}),
    )

    second = FocusIndex(root, cache, include_vanilla=False)
    parsed = _spy_parse(second, monkeypatch)
    second.ensure_fresh()
    assert parsed == [[_rel(_FOCUS_DIR, "good")]]  # bad is a known error: not reparsed
    assert second.parse_errors() == expected_errors
    assert second.resolve("TST_good") is not None

    third = FocusIndex(root, cache, include_vanilla=False)
    parsed = _spy_parse(third, monkeypatch)
    third.ensure_fresh()
    assert parsed == []
    assert third.parse_errors() == expected_errors


def test_repaired_error_file_recovers_when_source_is_fixed(tmp_path):
    root = tmp_path / "FocusMod3"
    bad = root / _FOCUS_DIR / "bad.txt"
    _write_file(bad, "focus_tree = { focus = { id = TST_bad x = {{{", 1)
    cache = tmp_path / "cache"
    first = FocusIndex(root, cache, include_vanilla=False)
    first.ensure_fresh()
    assert first.parse_errors()

    _write_file(bad, "focus_tree = { focus = { id = TST_bad x = 1 y = 0 } }", 10)
    second = FocusIndex(root, cache, include_vanilla=False)
    second._stale_check.force_next()
    second.ensure_fresh()
    assert second.parse_errors() == []
    assert second.resolve("TST_bad") is not None


# ----- sharded fast path (recovery must not regress it) ------------------------


@pytest.mark.parametrize(
    "bad_records",
    [
        [{"lang": "en", "key": ["bad"]}],
        [{}],
        [{"lang": None, "key": "K_b"}],
        [{"lang": "en", "key": None}],
        "missing_records",
    ],
)
def test_sharded_unusable_records_reparse_only_that_file(tmp_path, monkeypatch, bad_records):
    root = tmp_path / "LocMod"
    _write_file(root / _LOC_DIR / "a_l_english.yml", 'l_english:\n K_a: "a"\n', 1)
    _write_file(root / _LOC_DIR / "b_l_english.yml", 'l_english:\n K_b: "b"\n', 2)
    cache = tmp_path / "cache"
    first = LocalisationIndex(root, cache, include_vanilla=False)
    first.ensure_fresh()
    b = str(Path(_LOC_DIR) / "b_l_english.yml")
    if bad_records == "missing_records":
        first._cache.shard_path(b).write_text(json.dumps({"relpath": b}), encoding="utf-8")
    else:
        first._cache.save_shard(b, bad_records, None)

    second = LocalisationIndex(root, cache, include_vanilla=False)
    parsed = _spy_parse(second, monkeypatch)
    second.ensure_fresh()
    assert parsed == [[b]]
    rec_a = second.resolve("K_a")
    rec_b = second.resolve("K_b")
    assert rec_a is not None and rec_b is not None
    assert rec_a["value"] == "a"
    assert rec_b["value"] == "b"

    third = LocalisationIndex(root, cache, include_vanilla=False)
    parsed = _spy_parse(third, monkeypatch)
    third.ensure_fresh()
    assert parsed == []
    assert _state(third) == _scratch_state(LocalisationIndex, root, tmp_path, "shard")
