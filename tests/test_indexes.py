"""Index tests — exercise both the fake mod root and the real one when available."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from md_mcp.indexes import FocusIndex, GenericTxtIndex, IdeaIndex, LocalisationIndex
from md_mcp.indexes.base import FileSig, IndexCache

_DUP_IDEA = "ideas = {\n\tcountry = {\n\t\tTST_dup = { picture = generic_idea }\n\t}\n}\n"
_DUP_IDEA_3WAY = "ideas = {\n\tcountry = {\n\t\tTST_dup3 = { picture = generic_idea }\n\t}\n}\n"
_DUP_IDEA_SAME_FILE = (
    "ideas = {\n"
    "\tcountry = {\n"
    "\t\tTST_dup_same_file = { picture = generic_idea }\n"
    "\t\tTST_dup_same_file = { picture = generic_idea }\n"
    "\t}\n"
    "}\n"
)
_DUP_TWO_KEYS = (
    "ideas = {\n"
    "\tcountry = {\n"
    "\t\tTST_dup_x = { picture = generic_idea }\n"
    "\t\tTST_dup_y = { picture = generic_idea }\n"
    "\t}\n"
    "}\n"
)


def _parse_tuple_index_file(abs_path: str, relpath: str) -> list[dict]:
    return [{"lang": "en", "key": Path(abs_path).read_text(encoding="utf-8").strip()}]


class _TupleIndex(GenericTxtIndex):
    cache_name = "tuple"
    subdirs = ("common/first", "common/second")
    patterns = ("*.txt", "*.yml")
    primary_key = ("lang", "key")
    parser_fn = staticmethod(_parse_tuple_index_file)


def test_generic_index_collects_multiple_dirs_patterns_and_tuple_keys(tmp_path, cache_dir):
    root = tmp_path / "TupleMod"
    first = root / "common" / "first"
    second = root / "common" / "second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    first_file = first / "first.txt"
    first_file.write_text("first", encoding="utf-8")
    (second / "second.yml").write_text("second", encoding="utf-8")
    (first / "ignored.json").write_text("ignored", encoding="utf-8")

    index = _TupleIndex(root, cache_dir, include_vanilla=False)
    index.ensure_fresh()

    assert index.list_files() == ["common/first/first.txt", "common/second/second.yml"]
    assert index.list_keys() == [("en", "first"), ("en", "second")]
    second_record = index.resolve(("en", "second"))
    assert second_record is not None
    assert second_record["file"] == "common/second/second.yml"

    first_file.write_text("updated", encoding="utf-8")
    index._stale_check.force_next()
    index.ensure_fresh()
    assert index.resolve(("en", "first")) is None
    updated_record = index.resolve(("en", "updated"))
    assert updated_record is not None
    assert updated_record["file"] == "common/first/first.txt"


def test_focus_index_builds_from_fixture(fake_mod_root, cache_dir):
    fi = FocusIndex(fake_mod_root, cache_dir)
    fi.ensure_fresh()

    ids = fi.list_ids()
    assert "TST_root" in ids
    assert "TST_branch_a" in ids
    assert "TST_branch_b" in ids
    assert "TST_shared" in ids


def test_focus_index_resolve_returns_path_and_line(fake_mod_root, cache_dir):
    fi = FocusIndex(fake_mod_root, cache_dir)
    rec = fi.resolve("TST_root")
    assert rec is not None
    assert rec["file"] == "common/national_focus/test.txt"
    assert rec["kind"] == "focus_tree"
    assert rec["line"] is not None and rec["line"] > 0


def test_focus_index_warm_path_is_noop(fake_mod_root, cache_dir):
    fi = FocusIndex(fake_mod_root, cache_dir)
    fi.ensure_fresh()
    files_before = fi.list_files()

    # Force a re-check; nothing changed, so the in-memory state should remain.
    fi._stale_check.force_next()
    fi.ensure_fresh()
    assert fi.list_files() == files_before


def test_focus_index_cache_persisted(fake_mod_root, cache_dir):
    fi = FocusIndex(fake_mod_root, cache_dir)
    fi.ensure_fresh()
    assert (cache_dir / "v3" / "focus.manifest.json").exists()
    assert (cache_dir / "v3" / "focus.data.json").exists()

    # Second instance reads cache + ensures freshness; no exception, matching ids.
    fi2 = FocusIndex(fake_mod_root, cache_dir)
    fi2.ensure_fresh()
    assert sorted(fi2.list_ids()) == sorted(fi.list_ids())


@pytest.mark.parametrize(
    "sig",
    [
        FileSig(mtime_ns=7, size=3),
        FileSig(mtime_ns=0, size=0),
        FileSig(mtime_ns=-1, size=5),
        FileSig(mtime_ns=5, size=-1),
    ],
)
def test_load_manifest_round_trips(cache_dir, sig):
    cache = IndexCache(cache_dir, "focus", 2)
    cache.save_manifest({"a.txt": sig})
    assert cache.load_manifest() == {"a.txt": sig}


@pytest.mark.parametrize(
    "payload",
    [
        '{"a.txt": null}',
        '{"a.txt": []}',
        '{"a.txt": [1]}',
        '{"a.txt": ["x", 1]}',
        '{"a.txt": [null, 1]}',
        '{"a.txt": {"mtime_ns": 1, "size": 2}}',
        '{"a.txt": 12}',
        '{"a.txt": [true, 1]}',
        '{"a.txt": [1, false]}',
        '{"a.txt": [1.5, 5]}',
        '{"good.txt": [1, 2], "bad.txt": null}',
        '["a.txt"]',
        '"a.txt"',
        "null",
        "not json at all",
    ],
)
def test_load_manifest_rejects_malformed(cache_dir, payload):
    cache = IndexCache(cache_dir, "focus", 2)
    cache.dir.mkdir(parents=True)
    cache.manifest_path.write_text(payload, encoding="utf-8")
    assert cache.load_manifest() is None


def test_focus_index_rebuilds_on_shape_corrupt_manifest(fake_mod_root, cache_dir):
    fi = FocusIndex(fake_mod_root, cache_dir)
    fi.ensure_fresh()
    ids = sorted(fi.list_ids())
    assert ids

    manifest = cache_dir / "v3" / "focus.manifest.json"
    data = cache_dir / "v3" / "focus.data.json"
    manifest.write_text(
        '{"common/national_focus/test.txt": {"mtime_ns": 1, "size": 2}}',
        encoding="utf-8",
    )
    data.write_text('{"files": {}}', encoding="utf-8")

    fi2 = FocusIndex(fake_mod_root, cache_dir)
    fi2.ensure_fresh()
    assert sorted(fi2.list_ids()) == ids


def test_load_manifest_unreadable_returns_none(cache_dir, monkeypatch):
    cache = IndexCache(cache_dir, "focus", 2)
    cache.save_manifest({"a.txt": FileSig(mtime_ns=7, size=3)})
    import builtins

    real_open = builtins.open

    def _raising_open(path, *args, **kwargs):
        if str(path).endswith("focus.manifest.json"):
            raise OSError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _raising_open)
    assert cache.load_manifest() is None


def test_load_data_round_trips(cache_dir):
    cache = IndexCache(cache_dir, "focus", 2)
    payload = {"files": {"a.txt": [{"id": "x"}]}}
    cache.save_data(payload)
    assert cache.load_data() == payload


def test_load_data_unreadable_returns_none(cache_dir, monkeypatch):
    cache = IndexCache(cache_dir, "focus", 2)
    cache.save_data({"files": {}})
    import builtins

    real_open = builtins.open

    def _raising_open(path, *args, **kwargs):
        if str(path).endswith("focus.data.json"):
            raise OSError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _raising_open)
    assert cache.load_data() is None


def test_load_data_rejects_malformed(cache_dir):
    cache = IndexCache(cache_dir, "focus", 2)
    cache.dir.mkdir(parents=True)
    cache.data_path.write_text("not json at all", encoding="utf-8")
    assert cache.load_data() is None


def test_focus_index_reports_parse_errors_from_parallel_build(
    fake_mod_root, cache_dir, monkeypatch
):
    monkeypatch.delenv("MD_MCP_SERIAL_PARSE", raising=False)
    focus_dir = fake_mod_root / "common" / "national_focus"
    for number in range(3):
        (focus_dir / f"parallel_{number}.txt").write_text(
            f"focus_tree = {{ focus = {{ id = TST_parallel_{number} }} }}", encoding="utf-8"
        )
    (focus_dir / "parallel_broken.txt").write_text(
        "focus_tree = { focus = { id = TST_broken x = {{{", encoding="utf-8"
    )

    index = FocusIndex(fake_mod_root, cache_dir)
    index.ensure_fresh()

    errors = index.parse_errors()
    assert errors[0]["file"] == "common/national_focus/parallel_broken.txt"
    assert errors[0]["error"].startswith("parse failed:")


def test_loc_index_extracts_keys_and_values(fake_mod_root, cache_dir):
    li = LocalisationIndex(fake_mod_root, cache_dir)
    li.ensure_fresh()

    r = li.resolve("TST_root")
    assert r is not None
    assert r["value"] == "The Root Focus"
    assert r["lang"] == "en"
    assert r["file"].endswith("test_l_english.yml")
    assert r["line"] is not None


def test_loc_index_handles_embedded_quotes(fake_mod_root, cache_dir):
    li = LocalisationIndex(fake_mod_root, cache_dir)
    r = li.resolve("TST_with_quote")
    assert r is not None
    assert r["value"] == 'He called it "important"'


def test_loc_index_suppresses_duplicate_warnings_but_keeps_last_write_wins(
    tmp_path, cache_dir, caplog
):
    root = tmp_path / "DuplicateLocMod"
    loc_dir = root / "localisation" / "english"
    loc_dir.mkdir(parents=True)
    content = 'l_english:\n TST_duplicate_loc: "Value"\n'
    (loc_dir / "a_l_english.yml").write_text(content, encoding="utf-8")
    (loc_dir / "b_l_english.yml").write_text(content, encoding="utf-8")

    li = LocalisationIndex(root, cache_dir, include_vanilla=False)
    with caplog.at_level(logging.WARNING):
        li.ensure_fresh()

    record = li.resolve("TST_duplicate_loc")
    assert record is not None
    assert record["file"] == "localisation/english/b_l_english.yml"
    assert li.duplicates() == {
        ("l_english", "TST_duplicate_loc"): ["localisation/english/a_l_english.yml"]
    }
    assert not [r for r in caplog.records if "Duplicate key" in r.getMessage()]


def test_loc_index_rebuilds_stale_file(fake_mod_root, cache_dir):
    li = LocalisationIndex(fake_mod_root, cache_dir)
    assert li.resolve("TST_root") is not None

    loc_file = fake_mod_root / "localisation" / "english" / "test_l_english.yml"
    loc_file.write_text('l_english:\n TST_root: "Updated Root"\n', encoding="utf-8")
    li._stale_check.force_next()

    updated = li.resolve("TST_root")
    assert updated is not None
    assert updated["value"] == "Updated Root"


def test_loc_index_handles_legacy_version_suffix(fake_mod_root, cache_dir):
    li = LocalisationIndex(fake_mod_root, cache_dir)
    r = li.resolve("TST_versioned")
    assert r is not None
    assert r["value"] == "value with legacy version suffix"


def test_loc_index_parallel_build(fake_mod_root, cache_dir, monkeypatch):
    monkeypatch.delenv("MD_MCP_SERIAL_PARSE", raising=False)
    loc_dir = fake_mod_root / "localisation" / "english"
    for number in range(4):
        (loc_dir / f"parallel_{number}_l_english.yml").write_text(
            f'l_english:\n TST_parallel_{number}: "Parallel {number}"\n', encoding="utf-8"
        )

    li = LocalisationIndex(fake_mod_root, cache_dir)
    li.ensure_fresh()

    rec = li.resolve("TST_parallel_3")
    assert rec is not None
    assert rec["value"] == "Parallel 3"


def test_loc_index_fallback_to_english(fake_mod_root, cache_dir):
    li = LocalisationIndex(fake_mod_root, cache_dir)
    # No German fixture — should fall back to English.
    r = li.resolve("TST_root", lang="de")
    assert r is not None
    assert r["value"] == "The Root Focus"
    assert r["lang"] == "en"


def test_generic_index_duplicates_empty_by_default(fake_mod_root, cache_dir):
    idx = IdeaIndex(fake_mod_root, cache_dir, include_vanilla=False)
    idx.ensure_fresh()
    assert idx.duplicates() == {}


def test_generic_index_logs_duplicate_key(tmp_path, cache_dir, caplog):
    root = tmp_path / "DupMod"
    ideas_dir = root / "common" / "ideas"
    ideas_dir.mkdir(parents=True)
    (ideas_dir / "a_ideas.txt").write_text(_DUP_IDEA, encoding="utf-8")
    (ideas_dir / "b_ideas.txt").write_text(_DUP_IDEA, encoding="utf-8")

    idx = IdeaIndex(root, cache_dir, include_vanilla=False)
    with caplog.at_level(logging.WARNING):
        idx.ensure_fresh()

    rec = idx.resolve("TST_dup")
    assert rec is not None
    files = {"common/ideas/a_ideas.txt", "common/ideas/b_ideas.txt"}
    winner = rec["file"]
    shadowed = next(iter(files - {winner}))

    assert idx.duplicates() == {"TST_dup": [shadowed]}

    dup_warnings = [r for r in caplog.records if "TST_dup" in r.getMessage()]
    assert len(dup_warnings) == 1
    assert shadowed in dup_warnings[0].getMessage()
    assert winner in dup_warnings[0].getMessage()


def test_generic_index_three_way_duplicate_last_write_wins(tmp_path, cache_dir, caplog):
    root = tmp_path / "DupMod3"
    ideas_dir = root / "common" / "ideas"
    ideas_dir.mkdir(parents=True)
    (ideas_dir / "a_ideas.txt").write_text(_DUP_IDEA_3WAY, encoding="utf-8")
    (ideas_dir / "b_ideas.txt").write_text(_DUP_IDEA_3WAY, encoding="utf-8")
    (ideas_dir / "c_ideas.txt").write_text(_DUP_IDEA_3WAY, encoding="utf-8")

    idx = IdeaIndex(root, cache_dir, include_vanilla=False)
    with caplog.at_level(logging.WARNING):
        idx.ensure_fresh()

    # Canonical relpath order makes c_ideas.txt win.
    rec = idx.resolve("TST_dup3")
    assert rec is not None
    assert rec["file"] == "common/ideas/c_ideas.txt"
    assert idx.duplicates() == {
        "TST_dup3": ["common/ideas/a_ideas.txt", "common/ideas/b_ideas.txt"]
    }

    dup_warnings = [r for r in caplog.records if "TST_dup3" in r.getMessage()]
    assert len(dup_warnings) == 1


def test_generic_index_duplicate_within_single_file(tmp_path, cache_dir, caplog):
    root = tmp_path / "DupModSameFile"
    ideas_dir = root / "common" / "ideas"
    ideas_dir.mkdir(parents=True)
    (ideas_dir / "a_ideas.txt").write_text(_DUP_IDEA_SAME_FILE, encoding="utf-8")

    idx = IdeaIndex(root, cache_dir, include_vanilla=False)
    with caplog.at_level(logging.WARNING):
        idx.ensure_fresh()

    rec = idx.resolve("TST_dup_same_file")
    assert rec is not None
    assert rec["file"] == "common/ideas/a_ideas.txt"
    assert idx.duplicates() == {"TST_dup_same_file": ["common/ideas/a_ideas.txt"]}

    dup_warnings = [r for r in caplog.records if "TST_dup_same_file" in r.getMessage()]
    assert len(dup_warnings) == 1


def test_generic_index_duplicate_cleared_after_fix(tmp_path, cache_dir):
    root = tmp_path / "DupModFix"
    ideas_dir = root / "common" / "ideas"
    ideas_dir.mkdir(parents=True)
    a_file = ideas_dir / "a_ideas.txt"
    # pi-lens-ignore: python-path-traversal
    a_file.write_text(_DUP_IDEA, encoding="utf-8")
    (ideas_dir / "b_ideas.txt").write_text(_DUP_IDEA, encoding="utf-8")

    idx = IdeaIndex(root, cache_dir, include_vanilla=False)
    idx.ensure_fresh()
    assert "TST_dup" in idx.duplicates()

    # pi-lens-ignore: python-path-traversal
    a_file.write_text(
        "ideas = {\n\tcountry = {\n\t\tTST_no_longer_dup = { picture = generic_idea }\n\t}\n}\n",
        encoding="utf-8",
    )
    idx._stale_check.force_next()
    idx.ensure_fresh()

    assert "TST_dup" not in idx.duplicates()
    assert idx.resolve("TST_dup") is not None  # b_ideas.txt still defines it


def test_generic_index_duplicates_populated_on_cache_hit(tmp_path, cache_dir):
    root = tmp_path / "DupModCache"
    ideas_dir = root / "common" / "ideas"
    ideas_dir.mkdir(parents=True)
    (ideas_dir / "a_ideas.txt").write_text(_DUP_IDEA, encoding="utf-8")
    (ideas_dir / "b_ideas.txt").write_text(_DUP_IDEA, encoding="utf-8")

    idx1 = IdeaIndex(root, cache_dir, include_vanilla=False)
    idx1.ensure_fresh()
    assert "TST_dup" in idx1.duplicates()  # sanity: the fixture above does collide

    # A brand-new instance over the same cache dir loads from the persisted
    # cache (nothing stale, no reparse) rather than rebuilding from scratch.
    # Canonical relpath order makes b_ideas.txt win both times.
    idx2 = IdeaIndex(root, cache_dir, include_vanilla=False)
    dups2 = idx2.duplicates()
    assert "TST_dup" in dups2
    rec2 = idx2.resolve("TST_dup")
    assert rec2 is not None
    assert rec2["file"] == "common/ideas/b_ideas.txt"
    assert dups2["TST_dup"] == ["common/ideas/a_ideas.txt"]
    rec1 = idx1.resolve("TST_dup")
    assert rec1 is not None
    assert rec2["file"] == rec1["file"]  # cache hit matches cold build


def test_generic_index_duplicates_no_cross_key_interference(tmp_path, cache_dir, caplog):
    root = tmp_path / "DupModTwoKeys"
    ideas_dir = root / "common" / "ideas"
    ideas_dir.mkdir(parents=True)
    (ideas_dir / "a_ideas.txt").write_text(_DUP_TWO_KEYS, encoding="utf-8")
    (ideas_dir / "b_ideas.txt").write_text(_DUP_TWO_KEYS, encoding="utf-8")

    idx = IdeaIndex(root, cache_dir, include_vanilla=False)
    with caplog.at_level(logging.WARNING):
        idx.ensure_fresh()

    dups = idx.duplicates()
    assert set(dups.keys()) == {"TST_dup_x", "TST_dup_y"}
    assert len(dups["TST_dup_x"]) == 1
    assert len(dups["TST_dup_y"]) == 1

    x_warnings = [r for r in caplog.records if "TST_dup_x" in r.getMessage()]
    y_warnings = [r for r in caplog.records if "TST_dup_y" in r.getMessage()]
    assert len(x_warnings) == 1
    assert len(y_warnings) == 1


def test_duplicate_winner_is_deterministic_across_cold_builds_and_cache_hits(tmp_path):
    root = tmp_path / "DupModDet"
    ideas_dir = root / "common" / "ideas"
    ideas_dir.mkdir(parents=True)
    (ideas_dir / "a_ideas.txt").write_text(_DUP_IDEA, encoding="utf-8")
    (ideas_dir / "b_ideas.txt").write_text(_DUP_IDEA, encoding="utf-8")

    cache1 = tmp_path / "cache1"
    idx1 = IdeaIndex(root, cache1, include_vanilla=False)
    idx1.ensure_fresh()
    winner1 = idx1.resolve("TST_dup")
    assert winner1 is not None
    assert winner1["file"] == "common/ideas/b_ideas.txt"

    cache2 = tmp_path / "cache2"
    idx2 = IdeaIndex(root, cache2, include_vanilla=False)
    idx2.ensure_fresh()
    winner2 = idx2.resolve("TST_dup")
    assert winner2 is not None
    assert winner2["file"] == winner1["file"]

    idx3 = IdeaIndex(root, cache1, include_vanilla=False)
    winner3 = idx3.resolve("TST_dup")
    assert winner3 is not None
    assert winner3["file"] == winner1["file"]


@pytest.mark.integration
def test_focus_index_against_real_mod(real_mod_root, cache_dir):
    fi = FocusIndex(real_mod_root, cache_dir)
    fi.ensure_fresh()
    # Sanity floor — the mod should have at least 5000 focuses.
    assert len(fi.list_ids()) > 5000


@pytest.mark.integration
def test_loc_index_against_real_mod(real_mod_root, cache_dir):
    li = LocalisationIndex(real_mod_root, cache_dir)
    li.ensure_fresh()
    assert "TT_IF_THEY_ACCEPT" in li.list_keys("en")


def test_manifest_missing_returns_none(tmp_path):
    cache = IndexCache(tmp_path, "focus", 2)
    assert cache.load_manifest() is None
