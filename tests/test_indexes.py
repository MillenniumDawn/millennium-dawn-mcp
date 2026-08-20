"""Index tests — exercise both the fake mod root and the real one when available."""

from __future__ import annotations

# pi-lens-ignore: reportMissingImports
import pytest

from md_mcp.indexes import FocusIndex, LocalisationIndex


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
    assert (cache_dir / "v1" / "focus.manifest.json").exists()
    assert (cache_dir / "v1" / "focus.data.json").exists()

    # Second instance reads cache + ensures freshness; no exception, matching ids.
    fi2 = FocusIndex(fake_mod_root, cache_dir)
    fi2.ensure_fresh()
    assert sorted(fi2.list_ids()) == sorted(fi.list_ids())


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
