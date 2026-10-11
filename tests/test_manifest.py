"""list_country_content tests."""

from __future__ import annotations

from typing import cast

from md_mcp.analysis.manifest import _scan_files, list_country_content
from md_mcp.indexes import (
    CountryTagIndex,
    DecisionIndex,
    EventIndex,
    FocusIndex,
    IdeaIndex,
    LocalisationIndex,
)
from md_mcp.indexes.event import _parse_event_file


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
    history = fake_mod_root / "history" / "countries"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)
    (history / "TST - Testland.txt").write_text("capital = 1\n", encoding="utf-8")
    (history / "TST - Testland 2.txt").write_text("capital = 2\n", encoding="utf-8")
    (history / "TSTISH - Testland.txt").write_text("capital = 3\n", encoding="utf-8")
    event = fake_mod_root / "events" / "Testland.txt"
    event.write_text(
        "add_namespace = Testland\ncountry_event = { id = Testland.1 }\n",
        encoding="utf-8",
    )
    shared_event = fake_mod_root / "events" / "shared_events.txt"
    shared_event.write_text(
        "add_namespace = Common\ncountry_event = { id = Common.1\n\ttrigger = { tag = TST }\n}\n",
        encoding="utf-8",
    )

    class CacheFreeEvents:
        def __init__(self, records):
            self.records = {item["id"]: item for item in records}

        def ensure_fresh(self):
            pass

        def list_keys(self):
            return list(self.records)

        def resolve(self, key):
            return self.records.get(key)

    parsed_events = [
        {**item, "file": "events/Testland.txt"}
        for item in (_parse_event_file(str(event), "events/Testland.txt") or [])
    ]
    parsed_events.extend(
        {**item, "file": "events/shared_events.txt"}
        for item in (_parse_event_file(str(shared_event), "events/shared_events.txt") or [])
    )
    event_adapter = CacheFreeEvents(parsed_events)
    tags = CountryTagIndex(fake_mod_root, cache_dir, include_vanilla=False)

    result = list_country_content(
        "TST",
        fake_mod_root,
        country_tag_index=tags,
        event_index=cast(EventIndex, event_adapter),
        include=["events", "event_files", "history_files"],
    )

    assert result["event_files"] == ["events/Testland.txt"]
    assert result["history_files"] == [
        "history/countries/TST - Testland 2.txt",
        "history/countries/TST - Testland.txt",
    ]
    assert result["events"] == ["Testland.1"]
    assert "Common.1" not in result["events"]
    assert result["counts"]["history_files"] == len(result["history_files"])
    assert result["counts"]["event_files"] == len(result["event_files"])


def test_history_tag_separator_is_independent_of_country_long_name(fake_mod_root, cache_dir):
    tags_file = fake_mod_root / "common" / "country_tags" / "test_tags.txt"
    tags_file.write_text(
        'TST = "countries/Testland.txt"\nPER = "countries/Persia.txt"\n',
        encoding="utf-8",
    )
    history = fake_mod_root / "history" / "countries"
    history.mkdir(parents=True, exist_ok=True)
    for name in ("PER - Iran.txt", "PER - Iran 2.txt", "PERF - Iran.txt"):
        (history / name).write_text("capital = 1\n", encoding="utf-8")

    result = list_country_content(
        "PER",
        fake_mod_root,
        country_tag_index=CountryTagIndex(fake_mod_root, cache_dir, include_vanilla=False),
        include=["history_files"],
    )

    assert result["history_files"] == [
        "history/countries/PER - Iran 2.txt",
        "history/countries/PER - Iran.txt",
    ]
    assert result["counts"]["history_files"] == 2
