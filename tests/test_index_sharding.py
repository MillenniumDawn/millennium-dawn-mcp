"""Sharded cache storage and incremental `_rebuild` (issue #172)."""

from __future__ import annotations

import json
import logging
import os
import random
from pathlib import Path

import pytest

from md_mcp.indexes import FocusIndex, IdeaIndex, LocalisationIndex
from md_mcp.indexes.base import IndexCache, shard_filename

_LOC_DIR = Path("localisation") / "english"


def _loc(root: Path, name: str) -> Path:
    return root / _LOC_DIR / f"{name}_l_english.yml"


def _write(path: Path, text: str, step: int) -> None:
    """Write `path` and give it a distinct mtime so the signature always moves."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    ns = 1_700_000_000_000_000_000 + step * 1_000_000_000
    os.utime(path, ns=(ns, ns))


def _loc_text(entries: dict[str, str]) -> str:
    return "l_english:\n" + "".join(f' {k}: "{v}"\n' for k, v in entries.items())


@pytest.fixture
def loc_root(tmp_path: Path) -> Path:
    """Mod root with several loc files that share keys, so shadowing is exercised."""
    root = tmp_path / "LocMod"
    _write(_loc(root, "a"), _loc_text({"K_shared": "from a", "K_only_a": "a"}), 1)
    _write(_loc(root, "b"), _loc_text({"K_shared": "from b", "K_only_b": "b", "K_x": "b"}), 2)
    _write(_loc(root, "c"), _loc_text({"K_shared": "from c", "K_x": "c"}), 3)
    _write(_loc(root, "d"), _loc_text({"K_only_d": "d"}), 4)
    return root


def _mk(root: Path, cache: Path) -> LocalisationIndex:
    return LocalisationIndex(root, cache, include_vanilla=False)


def _state(idx) -> tuple:
    idx.ensure_fresh()
    return (
        list(idx._by_file.items()),
        idx._by_key,
        idx._duplicates,
        idx._parse_errors,
    )


def _scratch_state(cls, root: Path, tmp_path: Path, tag: str, **kwargs) -> tuple:
    """State of a brand-new instance with an empty cache: the ground truth."""
    return _state(cls(root, tmp_path / f"scratch-{tag}", include_vanilla=False, **kwargs))


def _spy_parse(idx, monkeypatch) -> list[list[str]]:
    """Record the relpaths each `_parse_parallel` call is asked to parse."""
    calls: list[list[str]] = []
    real = idx._parse_parallel

    def spy(relpaths: list[str]) -> list:
        calls.append(list(relpaths))
        return real(relpaths)

    monkeypatch.setattr(idx, "_parse_parallel", spy)
    return calls


def _shard_mtimes(idx) -> dict[str, int]:
    return {p.name: p.stat().st_mtime_ns for p in idx._cache.shard_dir.iterdir()}


# ----- sharded cache ---------------------------------------------------------


def test_shard_filename_is_stable_safe_and_unique():
    a = shard_filename(str(Path("localisation/english/a_l_english.yml")))
    assert a == shard_filename(str(Path("localisation/english/a_l_english.yml")))
    assert a != shard_filename(str(Path("localisation/german/a_l_english.yml")))
    assert "/" not in a and a.endswith(".json")
    assert shard_filename("x/ünï cödé?.yml").isascii()


def test_sharded_index_writes_one_shard_per_file_and_no_monolith(loc_root, cache_dir):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    cache = idx._cache
    assert not cache.data_path.exists()
    assert cache.manifest_path.exists()
    assert len(list(cache.shard_dir.iterdir())) == 4
    assert cache.dir.name == "v3"


def test_startup_from_shards_equals_full_rebuild(loc_root, cache_dir, tmp_path):
    _mk(loc_root, cache_dir).ensure_fresh()
    reloaded = _mk(loc_root, cache_dir)
    assert _state(reloaded) == _scratch_state(LocalisationIndex, loc_root, tmp_path, "eq")


def test_startup_from_shards_does_not_reparse(loc_root, cache_dir, monkeypatch):
    _mk(loc_root, cache_dir).ensure_fresh()
    reloaded = _mk(loc_root, cache_dir)
    calls = _spy_parse(reloaded, monkeypatch)
    reloaded.ensure_fresh()
    assert all(not rels for rels in calls)
    assert reloaded.resolve("K_shared")["value"] == "from c"  # type: ignore[index]


def test_one_file_edit_rewrites_exactly_one_shard(loc_root, cache_dir):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    before = _shard_mtimes(idx)
    # Make "unchanged" detectable even on coarse-mtime filesystems.
    for p in idx._cache.shard_dir.iterdir():
        os.utime(p, ns=(1, 1))
    before = _shard_mtimes(idx)

    _write(_loc(loc_root, "b"), _loc_text({"K_shared": "from b2", "K_only_b": "b2"}), 10)
    idx._stale_check.force_next()
    idx.ensure_fresh()

    after = _shard_mtimes(idx)
    assert set(after) == set(before)
    changed = {name for name in after if after[name] != before[name]}
    assert changed == {shard_filename(str(Path("localisation/english/b_l_english.yml")))}
    assert idx.resolve("K_only_b")["value"] == "b2"  # type: ignore[index]


def test_removed_file_deletes_its_shard(loc_root, cache_dir):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    shard = idx._cache.shard_path(str(Path("localisation/english/d_l_english.yml")))
    assert shard.exists()

    _loc(loc_root, "d").unlink()
    idx._stale_check.force_next()
    idx.ensure_fresh()
    assert not shard.exists()
    assert idx.resolve("K_only_d") is None
    # A fresh process agrees.
    assert _state(_mk(loc_root, cache_dir)) == _state(idx)


def test_removed_file_while_server_down_deletes_shard_on_next_start(loc_root, cache_dir):
    _mk(loc_root, cache_dir).ensure_fresh()
    _loc(loc_root, "d").unlink()
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    assert len(list(idx._cache.shard_dir.iterdir())) == 3


def test_orphan_shards_are_pruned_on_rebuild(loc_root, cache_dir):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    orphan = idx._cache.shard_dir / "stale-0000.json"
    orphan.write_text("junk", encoding="utf-8")
    # Another process's in-flight write must not be pruned out from under it.
    in_flight = idx._cache.shard_dir / "other-0000.json.4242.tmp"
    in_flight.write_text("partial", encoding="utf-8")
    _write(_loc(loc_root, "a"), _loc_text({"K_only_a": "edited"}), 20)
    _mk(loc_root, cache_dir).ensure_fresh()
    assert not orphan.exists()
    assert in_flight.exists()


def test_atomic_write_leaves_no_tmp_and_uses_a_per_process_name(cache_dir):
    from md_mcp.indexes.base import _atomic_write_text

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "x.json"
    _atomic_write_text(target, "{}")
    assert target.read_text(encoding="utf-8") == "{}"
    assert not list(cache_dir.glob("*.tmp"))


@pytest.mark.parametrize(
    "garbage",
    [
        "not json at all",
        "",
        "[]",
        '{"relpath": "some/other/file.yml", "records": []}',
        json.dumps(
            {"relpath": str(Path("localisation/english/c_l_english.yml")), "records": "nope"}
        ),
        json.dumps(
            {"relpath": str(Path("localisation/english/c_l_english.yml")), "records": [1, 2]}
        ),
        json.dumps(
            {
                "relpath": str(Path("localisation/english/c_l_english.yml")),
                "records": [],
                "error": 5,
            }
        ),
    ],
)
def test_corrupt_shard_reparses_only_that_file(loc_root, cache_dir, tmp_path, garbage, monkeypatch):
    first = _mk(loc_root, cache_dir)
    first.ensure_fresh()
    victim = first._cache.shard_path(str(Path("localisation/english/c_l_english.yml")))
    victim.write_text(garbage, encoding="utf-8")

    second = _mk(loc_root, cache_dir)
    parsed = _spy_parse(second, monkeypatch)
    second.ensure_fresh()

    assert parsed == [[str(Path("localisation/english/c_l_english.yml"))]]
    assert _state(second) == _scratch_state(LocalisationIndex, loc_root, tmp_path, "corrupt")
    # The shard was healed, so the next start parses nothing.
    third = _mk(loc_root, cache_dir)
    assert third._cache.load_shard(str(Path("localisation/english/c_l_english.yml"))) is not None
    assert _state(third) == _state(second)


def test_missing_shard_reparses_only_that_file(loc_root, cache_dir, tmp_path, monkeypatch):
    first = _mk(loc_root, cache_dir)
    first.ensure_fresh()
    first._cache.shard_path(str(Path("localisation/english/a_l_english.yml"))).unlink()

    second = _mk(loc_root, cache_dir)
    parsed = _spy_parse(second, monkeypatch)
    assert _state(second) == _scratch_state(LocalisationIndex, loc_root, tmp_path, "missing")
    assert parsed == [[str(Path("localisation/english/a_l_english.yml"))]]


def test_shard_round_trips_records_and_errors(cache_dir):
    cache = IndexCache(cache_dir, "x", 1)
    cache.save_shard("a/b.txt", [{"id": "A", "line": 1}], None)
    assert cache.load_shard("a/b.txt") == ([{"id": "A", "line": 1}], None)
    cache.save_shard("a/c.txt", None, "parse failed: boom")
    assert cache.load_shard("a/c.txt") == (None, "parse failed: boom")
    assert cache.load_shard("a/never.txt") is None


def test_monolithic_indexes_keep_data_json(fake_mod_root, cache_dir):
    idx = IdeaIndex(fake_mod_root, cache_dir, include_vanilla=False)
    idx.ensure_fresh()
    assert idx._cache.data_path.exists()
    assert not idx._cache.shard_dir.exists()


def test_sharded_parse_errors_survive_restart(tmp_path, cache_dir):
    """A sharded index that tracks parse errors reports them after a restart."""

    class ShardedFocus(FocusIndex):
        sharded = True

    root = tmp_path / "FocusMod"
    focus_dir = root / "common" / "national_focus"
    _write(focus_dir / "good.txt", "focus_tree = { focus = { id = TST_good x = 1 y = 0 } }", 1)
    _write(focus_dir / "bad.txt", "focus_tree = { focus = { id = TST_bad x = {{{", 2)

    first = ShardedFocus(root, cache_dir, include_vanilla=False)
    first.ensure_fresh()
    assert [e["file"] for e in first.parse_errors()] == [str(Path("common/national_focus/bad.txt"))]

    second = ShardedFocus(root, cache_dir, include_vanilla=False)
    assert second.parse_errors() == first.parse_errors()
    assert second.resolve("TST_good") is not None


# ----- incremental rebuild ---------------------------------------------------


def test_edit_one_file_matches_scratch_rebuild(loc_root, cache_dir, tmp_path):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    _write(_loc(loc_root, "c"), _loc_text({"K_x": "c2", "K_new": "n"}), 10)
    idx._stale_check.force_next()
    assert _state(idx) == _scratch_state(LocalisationIndex, loc_root, tmp_path, "edit")
    # c stopped defining K_shared, so b now wins it and a is the only shadowed file.
    assert idx.resolve("K_shared")["file"].endswith("b_l_english.yml")  # type: ignore[index]
    assert idx.duplicates()[("l_english", "K_shared")] == [
        str(Path("localisation/english/a_l_english.yml"))
    ]


def test_incremental_does_not_reload_or_rewrite_unrelated_state(loc_root, cache_dir, monkeypatch):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    untouched = idx._by_key[("l_english", "K_only_d")]
    monkeypatch.setattr(
        idx._cache, "load_shard", lambda *_: pytest.fail("warm refresh must not read shards")
    )
    _write(_loc(loc_root, "a"), _loc_text({"K_only_a": "edited"}), 10)
    idx._stale_check.force_next()
    idx.ensure_fresh()
    assert idx._by_key[("l_english", "K_only_d")] is untouched


def test_removing_shadowing_file_unshadows_duplicate(loc_root, cache_dir, tmp_path):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    assert idx.resolve("K_shared")["value"] == "from c"  # type: ignore[index]

    _loc(loc_root, "c").unlink()
    idx._stale_check.force_next()
    assert idx.resolve("K_shared")["value"] == "from b"  # type: ignore[index]
    assert idx.resolve("K_x")["value"] == "b"  # type: ignore[index]
    assert _state(idx) == _scratch_state(LocalisationIndex, loc_root, tmp_path, "rm")

    _loc(loc_root, "b").unlink()
    idx._stale_check.force_next()
    assert idx.resolve("K_shared")["value"] == "from a"  # type: ignore[index]
    assert idx.resolve("K_x") is None
    assert idx.duplicates() == {}


def test_adding_a_later_file_shadows_existing_key(loc_root, cache_dir, tmp_path):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    _write(_loc(loc_root, "z"), _loc_text({"K_shared": "from z"}), 10)
    idx._stale_check.force_next()
    assert idx.resolve("K_shared")["value"] == "from z"  # type: ignore[index]
    assert _state(idx) == _scratch_state(LocalisationIndex, loc_root, tmp_path, "add")
    assert idx.duplicates()[("l_english", "K_shared")] == [
        str(Path("localisation/english/a_l_english.yml")),
        str(Path("localisation/english/b_l_english.yml")),
        str(Path("localisation/english/c_l_english.yml")),
    ]


def test_adding_an_earlier_file_does_not_take_the_win(loc_root, cache_dir, tmp_path):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    _write(_loc(loc_root, "0first"), _loc_text({"K_shared": "from 0"}), 10)
    idx._stale_check.force_next()
    assert idx.resolve("K_shared")["value"] == "from c"  # type: ignore[index]
    assert _state(idx) == _scratch_state(LocalisationIndex, loc_root, tmp_path, "early")
    assert list(idx._by_file) == sorted(idx._by_file)


def test_same_file_duplicate_keys_survive_incremental(tmp_path, cache_dir):
    root = tmp_path / "SameFile"
    _write(_loc(root, "a"), _loc_text({"K": "1"}) + ' K: "2"\n', 1)
    _write(_loc(root, "b"), _loc_text({"K": "3"}), 2)
    idx = _mk(root, cache_dir)
    idx.ensure_fresh()
    _write(_loc(root, "b"), _loc_text({"K": "4", "K2": "x"}), 10)
    idx._stale_check.force_next()
    assert _state(idx) == _scratch_state(LocalisationIndex, root, tmp_path, "same")
    assert idx.resolve("K")["value"] == "4"  # type: ignore[index]


def test_incremental_rebuild_matches_scratch_over_random_edits(tmp_path, cache_dir):
    """Property-style: any sequence of edits/adds/removes ends in the scratch state."""
    rng = random.Random(172)
    root = tmp_path / "RandomMod"
    names = [f"f{i}" for i in range(6)]
    pool = [f"K_{i}" for i in range(10)]
    live: set[str] = set()

    def random_text() -> str:
        picked = rng.sample(pool, rng.randint(0, 5))
        picked += rng.sample(picked, rng.randint(0, min(2, len(picked))))  # same-file dupes
        return "l_english:\n" + "".join(f' {k}: "{rng.random()}"\n' for k in picked)

    idx = _mk(root, cache_dir)
    for name in names[:3]:
        _write(_loc(root, name), random_text(), 0)
        live.add(name)
    idx.ensure_fresh()

    for step in range(1, 41):
        name = rng.choice(names)
        if name in live and rng.random() < 0.3:
            _loc(root, name).unlink()
            live.discard(name)
        else:
            _write(_loc(root, name), random_text(), step)
            live.add(name)
        idx._stale_check.force_next()
        assert _state(idx) == _scratch_state(
            LocalisationIndex, root, tmp_path, f"r{step}"
        ), f"diverged at step {step}"
        # And what is on disk agrees too.
        assert _state(_mk(root, cache_dir)) == _state(idx), f"cache diverged at step {step}"


def test_incremental_equals_scratch_for_monolithic_index(tmp_path, cache_dir):
    root = tmp_path / "IdeaMod"
    ideas = root / "common" / "ideas"

    def idea(*ids: str) -> str:
        body = "".join(f"\t\t{i} = {{ picture = generic_idea }}\n" for i in ids)
        return "ideas = {\n\tcountry = {\n" + body + "\t}\n}\n"

    _write(ideas / "a.txt", idea("TST_dup", "TST_a"), 1)
    _write(ideas / "b.txt", idea("TST_dup", "TST_b"), 2)
    idx = IdeaIndex(root, cache_dir, include_vanilla=False)
    idx.ensure_fresh()
    assert idx.duplicates() == {"TST_dup": [str(Path("common/ideas/a.txt"))]}

    _write(ideas / "b.txt", idea("TST_b"), 10)
    idx._stale_check.force_next()
    assert _state(idx) == _scratch_state(IdeaIndex, root, tmp_path, "m1")
    assert idx.duplicates() == {}
    assert idx.resolve("TST_dup")["file"] == str(Path("common/ideas/a.txt"))  # type: ignore[index]

    (ideas / "a.txt").unlink()
    idx._stale_check.force_next()
    assert _state(idx) == _scratch_state(IdeaIndex, root, tmp_path, "m2")
    assert idx.resolve("TST_dup") is None
    # data.json was rewritten from the incremental state.
    assert _state(IdeaIndex(root, cache_dir, include_vanilla=False)) == _state(idx)


def test_incremental_duplicate_warning_fires_once_for_newly_duplicated_key(
    tmp_path, cache_dir, caplog
):
    root = tmp_path / "WarnMod"
    ideas = root / "common" / "ideas"
    body = "ideas = {\n\tcountry = {\n\t\tTST_w = { picture = generic_idea }\n\t}\n}\n"
    _write(ideas / "a.txt", body, 1)
    idx = IdeaIndex(root, cache_dir, include_vanilla=False)
    idx.ensure_fresh()
    assert not [r for r in caplog.records if "Duplicate key" in r.getMessage()]

    _write(ideas / "b.txt", body, 2)
    idx._stale_check.force_next()
    with caplog.at_level(logging.WARNING):
        idx.ensure_fresh()
    assert len([r for r in caplog.records if "Duplicate key" in r.getMessage()]) == 1


def test_incremental_tracks_parse_errors(tmp_path, cache_dir):
    root = tmp_path / "ErrMod"
    focus_dir = root / "common" / "national_focus"
    _write(focus_dir / "a.txt", "focus_tree = { focus = { id = TST_a x = 1 y = 0 } }", 1)
    idx = FocusIndex(root, cache_dir, include_vanilla=False)
    idx.ensure_fresh()
    assert idx.parse_errors() == []

    _write(focus_dir / "a.txt", "focus_tree = { focus = { id = TST_a x = {{{", 10)
    idx._stale_check.force_next()
    assert [e["file"] for e in idx.parse_errors()] == [str(Path("common/national_focus/a.txt"))]
    assert idx.resolve("TST_a") is None

    _write(focus_dir / "a.txt", "focus_tree = { focus = { id = TST_a x = 1 y = 0 } }", 20)
    idx._stale_check.force_next()
    assert idx.parse_errors() == []
    assert idx.resolve("TST_a") is not None
    assert _state(idx) == _scratch_state(FocusIndex, root, tmp_path, "err")


def test_unchanged_refresh_leaves_state_objects_alone(loc_root, cache_dir):
    idx = _mk(loc_root, cache_dir)
    idx.ensure_fresh()
    by_key, by_file = idx._by_key, idx._by_file
    idx._stale_check.force_next()
    idx.ensure_fresh()
    assert idx._by_key is by_key and idx._by_file is by_file


def test_one_file_edit_is_fast_on_a_large_loc_tree(tmp_path, cache_dir):
    """Smoke test of the scaling fix: work is per changed file, not per record."""
    import time

    root = tmp_path / "BigLoc"
    for f in range(40):
        entries = {f"BIG_{f}_{i}": f"value {i}" for i in range(1500)}
        _write(_loc(root, f"big{f:02d}"), _loc_text(entries), 1)
    idx = _mk(root, cache_dir)
    idx.ensure_fresh()
    assert len(idx._by_key) == 60_000

    _write(_loc(root, "big07"), _loc_text({"BIG_7_0": "edited"}), 10)
    idx._stale_check.force_next()
    t0 = time.perf_counter()
    idx.ensure_fresh()
    elapsed = time.perf_counter() - t0
    assert idx.resolve("BIG_7_0")["value"] == "edited"  # type: ignore[index]
    assert idx.resolve("BIG_7_1") is None
    assert elapsed < 1.0, f"one-file loc edit took {elapsed:.2f}s"
