"""Index tests — exercise both the fake mod root and the real one when available."""

from __future__ import annotations

import logging

import pytest

from md_mcp.indexes import FocusIndex, IdeaIndex, LocalisationIndex

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
    assert (cache_dir / "v2" / "focus.manifest.json").exists()
    assert (cache_dir / "v2" / "focus.data.json").exists()

    # Second instance reads cache + ensures freshness; no exception, matching ids.
    fi2 = FocusIndex(fake_mod_root, cache_dir)
    fi2.ensure_fresh()
    assert sorted(fi2.list_ids()) == sorted(fi.list_ids())


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


def test_loc_index_handles_legacy_version_suffix(fake_mod_root, cache_dir):
    li = LocalisationIndex(fake_mod_root, cache_dir)
    r = li.resolve("TST_versioned")
    assert r is not None
    assert r["value"] == "value with legacy version suffix"


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

    # Processing order isn't alphabetical (it comes from a set difference in
    # compute_staleness), so derive the expected order from the index's own
    # bookkeeping rather than assuming a file name wins.
    scan_order = list(idx._by_file.keys())
    rec = idx.resolve("TST_dup3")
    assert rec is not None
    assert scan_order[-1] == rec["file"]
    assert idx.duplicates() == {"TST_dup3": scan_order[:-1]}

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
    a_file.write_text(_DUP_IDEA, encoding="utf-8")
    (ideas_dir / "b_ideas.txt").write_text(_DUP_IDEA, encoding="utf-8")

    idx = IdeaIndex(root, cache_dir, include_vanilla=False)
    idx.ensure_fresh()
    assert "TST_dup" in idx.duplicates()

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
    # `_rebuild` still recomputes duplicates from the cached per-file records,
    # so this must report the collision too, not silently return {}. (The
    # winner itself isn't pinned here: unlike the cache-hit path, which walks
    # files in on-disk manifest order, a from-scratch build's file order comes
    # from a set difference in compute_staleness and isn't guaranteed to match.)
    idx2 = IdeaIndex(root, cache_dir, include_vanilla=False)
    dups2 = idx2.duplicates()
    assert "TST_dup" in dups2
    rec2 = idx2.resolve("TST_dup")
    assert rec2 is not None
    files = {"common/ideas/a_ideas.txt", "common/ideas/b_ideas.txt"}
    assert dups2["TST_dup"] == [f for f in files if f != rec2["file"]]


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
