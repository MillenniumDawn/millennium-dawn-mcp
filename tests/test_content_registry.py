from md_mcp.content_registry import (
    CONTENT_BY_NAME,
    INDEX_TYPES,
    MANIFEST_CATEGORIES,
    classify_content_path,
)


def test_registry_has_unique_canonical_content_names():
    names = [content.name for content in CONTENT_BY_NAME.values()]
    assert len(names) == len(set(names))
    assert all(item.index_name and item.index_attribute for item in INDEX_TYPES)


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
