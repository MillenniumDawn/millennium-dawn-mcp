import runpy
from pathlib import Path

from md_mcp.content_registry import (
    AUDIT_DEFAULT_KINDS,
    AUDIT_NOT_CHECKED,
    AUDIT_RESOLVER_INDEX_ATTRS,
    CONTENT_BY_NAME,
    CONTENT_TYPES,
    INDEX_TYPES,
    MANIFEST_CATEGORIES,
    REFERENCE_EXTENSIONS,
    REFERENCE_KIND_ALIASES,
    REFERENCE_PATTERNS,
    REFERENCE_SCAN_DIRS,
    ContentType,
    build_registry_views,
    classify_content_path,
    classify_registered_path,
    prepare_content_types,
)


def test_registry_has_unique_canonical_content_names():
    names = [content.name for content in CONTENT_TYPES]
    assert len(names) == len(set(names))
    assert set(names) == set(CONTENT_BY_NAME)
    assert all(item.index_name and item.index_attribute for item in INDEX_TYPES)


def test_reference_registry_is_complete_and_aliases_are_canonical():
    canonical = set(REFERENCE_PATTERNS)
    assert canonical == set(REFERENCE_SCAN_DIRS)
    assert set(REFERENCE_EXTENSIONS) <= canonical
    assert all(target in canonical for target in REFERENCE_KIND_ALIASES.values())
    assert all(source not in canonical for source in REFERENCE_KIND_ALIASES)
    assert set(AUDIT_DEFAULT_KINDS) <= set(REFERENCE_PATTERNS)
    assert set(AUDIT_RESOLVER_INDEX_ATTRS) <= set(REFERENCE_PATTERNS)


def test_registry_path_classification_preserves_analysis_kinds():
    paths = {
        "common/national_focus/a.txt": "focus",
        "events/a.txt": "event",
        "common/decisions/a.txt": "decision",
        "common/ideas/a.txt": "idea",
        "localisation/english/a.yml": "loc",
        "interface/a.gfx": "gfx",
        "interface/a.gui": "other",
        "history/countries/a.txt": "history",
    }
    assert {path: classify_content_path(path) for path in paths} == paths


def test_manifest_categories_are_unique():
    assert len(MANIFEST_CATEGORIES) == len(set(MANIFEST_CATEGORIES))
    assert AUDIT_NOT_CHECKED == (
        "country_flags",
        "variables",
        "scripted_effects",
        "scripted_triggers",
    )


def test_synthetic_content_entry_routes_consumers_and_fixture_builder(tmp_path, monkeypatch):
    from md_mcp.analysis import diff_summary, encoding, manifest, refs
    from md_mcp.analysis.ref_audit import check_refs
    from md_mcp.indexes import (
        DecisionIndex,
        EventIndex,
        FocusIndex,
        GfxIndex,
        IdeaIndex,
        LocalisationIndex,
    )

    build_fake_mod = runpy.run_path(str(Path(__file__).with_name("conftest.py")))["_build_fake_mod"]
    synthetic = ContentType(
        "synthetic",
        index_name="FocusIndex",
        index_attribute="synthetic_index",
        index_class=CONTENT_BY_NAME["focus"].index_class,
        path_prefixes=("common/synthetic/",),
        diff_extractor="focus",
        manifest_categories=("synthetic_ids", "synthetic_files"),
        manifest_extractor="indexed_country_records",
        reference_pattern=REFERENCE_PATTERNS["focus"],
        reference_dirs=("common/synthetic",),
        reference_extensions=(".txt",),
        encoding_extensions=(".txt",),
        encoding_dirs=("common/synthetic",),
        encoding_order=4,
        audit_default=False,
        audit_default_order=11,
        audit_resolver=True,
        audit_symbol_nodes=("synthetic_reference",),
        not_checked_label="synthetic_refs",
        not_checked_order=11,
    )
    entries = prepare_content_types((*CONTENT_TYPES, synthetic))
    views = build_registry_views(entries)
    assert classify_registered_path("common/synthetic/refs.txt", entries) == "synthetic"
    assert views["index_types"][-1] == synthetic
    assert views["reference_patterns"]["synthetic"] is synthetic.reference_pattern
    assert views["reference_scan_dirs"]["synthetic"] == ("common/synthetic",)
    assert views["manifest_categories"][-2:] == ("synthetic_ids", "synthetic_files")
    assert views["encoding_text_dirs"][-1] == "common/synthetic"
    assert views["audit_resolvers"]["synthetic"] == "synthetic_index"
    assert views["audit_symbol_kinds"][-1] == "synthetic"
    assert "synthetic" not in views["audit_default_kinds"]
    assert views["not_checked"][-1] == "synthetic_refs"

    monkeypatch.setattr(refs, "_PATTERNS", views["reference_patterns"])
    monkeypatch.setattr(refs, "_SCAN_DIRS", views["reference_scan_dirs"])
    monkeypatch.setattr(refs, "_EXTENSIONS", views["reference_extensions"])
    mod = build_fake_mod(tmp_path / "FakeMod", entries)
    assert (mod / "common/synthetic").is_dir()
    source = mod / "common/synthetic/refs.txt"
    source.write_text("focus = SYNTH_ref\n", encoding="utf-8")
    matches = refs.find_references(mod, "synthetic", "SYNTH_ref")
    assert matches["total"] == 1
    monkeypatch.setattr(manifest, "_ALL_CATEGORIES", views["manifest_categories"])
    monkeypatch.setattr(manifest, "CONTENT_TYPES", entries)
    assert "synthetic_ids" in manifest._resolve_include(["synthetic_ids"])

    class SyntheticIndex:
        def ids_for_country_tag(self, tag):
            return ["TST_synthetic_id"] if tag == "TST" else []

        def resolve(self, key):
            if key == "TST_synthetic_id":
                return {"file": "common/synthetic/TST.txt"}
            return None

    manifest_out = manifest.list_country_content(
        "TST",
        mod,
        registry_indexes={"synthetic_index": SyntheticIndex()},
        include=["synthetic_ids", "synthetic_files"],
    )
    assert manifest_out["synthetic_ids"] == ["TST_synthetic_id"]
    assert manifest_out["synthetic_files"] == ["common/synthetic/TST.txt"]
    assert encoding.check_encoding(mod)["total"] == 0
    monkeypatch.setattr(encoding, "_TXT_DIRS", views["encoding_text_dirs"])
    (mod / "common/synthetic/bom.txt").write_bytes(b"\xef\xbb\xbfcontent")
    assert encoding.check_encoding(mod)["violations"][0]["file"] == "common/synthetic/bom.txt"

    monkeypatch.setattr(diff_summary, "CONTENT_BY_NAME", views["by_name"])
    assert diff_summary._extract_ids("focus_tree = { focus = { id = SYNTH_id } }", "synthetic") == [
        "SYNTH_id"
    ]

    from md_mcp.analysis import ref_audit

    class UnresolvedSyntheticIndex:
        @staticmethod
        def parse_errors():
            return []

        @staticmethod
        def resolve(_key):
            return None

    monkeypatch.setattr(ref_audit, "CONTENT_TYPES", entries)
    audit_file = mod / "common/synthetic/audit.txt"
    audit_file.write_text("synthetic_reference = MISSING_id\n", encoding="utf-8")
    cache = tmp_path / "cache"
    audit = check_refs(
        mod,
        focus_index=FocusIndex(mod, cache),
        event_index=EventIndex(mod, cache, None, include_vanilla=False),
        idea_index=IdeaIndex(mod, cache, include_vanilla=False),
        gfx_index=GfxIndex(mod, cache, None),
        loc_index=LocalisationIndex(mod, cache, None),
        decision_index=DecisionIndex(mod, cache, None, include_vanilla=False),
        registry_indexes={"synthetic_index": UnresolvedSyntheticIndex()},
        files=["common/synthetic/audit.txt"],
        kinds=["synthetic"],
    )
    assert audit["unresolved"][0]["kind"] == "synthetic"
    assert audit["unresolved"][0]["ref"] == "MISSING_id"
    default_audit = check_refs(
        mod,
        focus_index=FocusIndex(mod, cache),
        event_index=EventIndex(mod, cache, None, include_vanilla=False),
        idea_index=IdeaIndex(mod, cache, include_vanilla=False),
        gfx_index=GfxIndex(mod, cache, None),
        loc_index=LocalisationIndex(mod, cache, None),
        decision_index=DecisionIndex(mod, cache, None, include_vanilla=False),
        registry_indexes={"synthetic_index": UnresolvedSyntheticIndex()},
        files=["common/synthetic/audit.txt"],
    )
    assert "synthetic" not in default_audit["kinds_checked"]
