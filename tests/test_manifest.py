"""list_country_content tests."""

from __future__ import annotations

from md_mcp.analysis.manifest import _scan_files, list_country_content
from md_mcp.indexes import (
    CountryTagIndex,
    DecisionIndex,
    EventIndex,
    FocusIndex,
    IdeaIndex,
    LocalisationIndex,
)


def _build_indexes(fake_mod_root, cache_dir):
    return {
        "focus_index": FocusIndex(fake_mod_root, cache_dir),
        "event_index": EventIndex(fake_mod_root, cache_dir, include_vanilla=False),
        "decision_index": DecisionIndex(fake_mod_root, cache_dir, include_vanilla=False),
        "idea_index": IdeaIndex(fake_mod_root, cache_dir, include_vanilla=False),
        "loc_index": LocalisationIndex(fake_mod_root, cache_dir),
    }


def test_manifest_default_returns_counts_only(fake_mod_root, cache_dir):
    """No include= argument → counts + tiny samples, no full lists."""
    result = list_country_content("TST", fake_mod_root, **_build_indexes(fake_mod_root, cache_dir))
    assert result["ok"]
    assert result["tag"] == "TST"
    assert "counts" in result
    assert result["counts"]["focuses"] >= 1
    # Default mode omits the heavy arrays entirely; samples may be present.
    assert "focuses" not in result
    assert "decisions" not in result


def test_manifest_include_specific_categories(fake_mod_root, cache_dir):
    """`include=[focuses, decisions]` returns those categories in full, counts for others."""
    result = list_country_content(
        "TST",
        fake_mod_root,
        include=["focuses", "decisions", "ideas"],
        **_build_indexes(fake_mod_root, cache_dir),
    )
    assert result["tag"] == "TST"
    assert "TST_root" in result["focuses"]
    assert "TST_simple_decision" in result["decisions"]
    assert "TST_simple_idea" in result["ideas"]
    # event_files not requested.
    assert "event_files" not in result


def test_manifest_include_wildcard(fake_mod_root, cache_dir):
    """`include=['*']` returns every category."""
    result = list_country_content(
        "TST",
        fake_mod_root,
        include=["*"],
        **_build_indexes(fake_mod_root, cache_dir),
    )
    assert "focuses" in result
    assert "decisions" in result
    assert "ideas" in result
    assert "loc_files" in result


def test_manifest_limit_per_category(fake_mod_root, cache_dir):
    """`limit_per_category` caps each returned list."""
    result = list_country_content(
        "TST",
        fake_mod_root,
        include=["focuses"],
        limit_per_category=1,
        **_build_indexes(fake_mod_root, cache_dir),
    )
    assert len(result["focuses"]) == 1
    assert result.get("focuses_truncated") is True


def test_scan_files_walks_submod_then_mod_with_dedupe(tmp_path):
    submod = tmp_path / "Overlay"
    mod = tmp_path / "Mod"
    for root in (submod, mod):
        d = root / "history" / "countries"
        d.mkdir(parents=True)
        (d / "TST_shared.txt").write_text("shared", encoding="utf-8")
    countries = mod / "history" / "countries"
    (countries / "TST_extra.txt").write_text("extra", encoding="utf-8")
    (countries / "history_TST.txt").write_text("suffix match", encoding="utf-8")
    (countries / "unrelated.txt").write_text("no match", encoding="utf-8")
    (countries / "TST_empty.txt").mkdir()  # rglob yields dirs; only files count

    out = _scan_files(mod, "history/countries", prefix="TST", submod_root=submod)

    assert out == [
        "history/countries/TST_extra.txt",
        "history/countries/TST_shared.txt",
        "history/countries/history_TST.txt",
    ]


def test_scan_files_skips_missing_roots(tmp_path):
    mod = tmp_path / "Mod"
    out = _scan_files(mod, "history/countries", prefix="TST")
    assert out == []


def test_manifest_uses_explicit_country_file_mapping_for_long_names(fake_mod_root, cache_dir):
    history = fake_mod_root / "history" / "countries" / "TST - Testland.txt"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text("capital = 1\n", encoding="utf-8")
    event = fake_mod_root / "events" / "Testland.txt"
    event.write_text(
        "add_namespace = Testland\ncountry_event = { id = Testland.1 }\n",
        encoding="utf-8",
    )
    tags = CountryTagIndex(fake_mod_root, cache_dir, include_vanilla=False)
    events = EventIndex(fake_mod_root, cache_dir, include_vanilla=False)

    result = list_country_content(
        "TST",
        fake_mod_root,
        country_tag_index=tags,
        event_index=events,
        include=["events", "event_files", "history_files"],
    )

    assert result["event_files"] == ["events/Testland.txt"]
    assert result["history_files"] == ["history/countries/TST - Testland.txt"]
    assert result["events"] == ["Testland.1"]
