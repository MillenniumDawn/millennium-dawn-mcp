"""list_country_content tests."""

from __future__ import annotations

from md_mcp.analysis.manifest import list_country_content
from md_mcp.indexes import (
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
