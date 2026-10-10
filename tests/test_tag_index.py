"""Tag-derived index lookups preserve the generic index's winning records."""

from __future__ import annotations

from pathlib import Path

from md_mcp.analysis.manifest import _ids_for_tag, _indexed_country_records
from md_mcp.indexes.base import GenericTxtIndex


def _parse_ids(abs_path: str, _relpath: str) -> list[dict]:
    return [
        {"id": line.strip()}
        for line in Path(abs_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _TagIndex(GenericTxtIndex):
    cache_name = "tag-test"
    subdir = "common/tags"
    parser_fn = staticmethod(_parse_ids)
    tag_indexed = True
    tag_file_indexed = True
    source_tag_indexed = True


class _ScanTagIndex(_TagIndex):
    cache_name = "tag-scan-test"
    tag_indexed = False
    tag_file_indexed = False
    source_tag_indexed = False


class _FileOnlyTagIndex(_TagIndex):
    cache_name = "tag-file-test"
    tag_indexed = False
    source_tag_indexed = False


class _SourceOnlyTagIndex(_TagIndex):
    cache_name = "tag-source-test"
    tag_indexed = False
    tag_file_indexed = False


class _TagOnlyIndex(_TagIndex):
    cache_name = "tag-only-test"
    tag_file_indexed = False
    source_tag_indexed = False


def _write(root: Path, relative_path: str, ids: list[str]) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(ids) + "\n", encoding="utf-8")
    return path


def _scan_ids(index: _TagIndex, tag: str) -> list[str]:
    prefix = tag.upper() + "_"
    return sorted(key for key in index.list_keys() if key.upper().startswith(prefix))


def _scan_country_records(index: _TagIndex, tag: str) -> tuple[list[str], list[str]]:
    tag_upper = tag.upper()
    prefix = tag_upper + "_"
    ids: set[str] = set()
    files: set[str] = set()
    for key in index.list_keys():
        record = index.resolve(key)
        assert record is not None
        file = str(record["file"])
        stem = Path(file).stem.upper()
        if key.upper().startswith(prefix) or stem == tag_upper or stem.startswith(prefix):
            ids.add(key)
            files.add(file)
    return sorted(ids), sorted(files)


def test_tag_lookups_match_case_insensitive_prefix_scan(tmp_path, cache_dir):
    root = tmp_path / "Mod"
    _write(root, "common/tags/a.txt", ["USA_dup", "USA_du_test", "UsA_case", "USA"])
    _write(root, "common/tags/z.txt", ["USA_dup", "usa_case2"])
    _write(root, "common/tags/usa_topics.txt", ["file_only", "USArmy_item"])
    _write(root, "common/tags/USA.txt", ["exact_file_only"])
    _write(root, "common/tags/USA_du_topics.txt", ["nested_file_only"])

    index = _TagIndex(root, cache_dir, include_vanilla=False)

    expected_ids = _scan_ids(index, "USA")
    assert expected_ids == ["USA_du_test", "USA_dup", "UsA_case", "usa_case2"]
    assert index.ids_for_tag("uSa") == expected_ids
    assert index.ids_for_tag("USA_du") == ["USA_du_test"]
    assert _ids_for_tag(index, "USA") == expected_ids
    assert _ids_for_tag(index, "USA_DU") == ["USA_du_test"]
    assert _ids_for_tag(None, "USA") == []
    assert index.files_for_tag("usa") == ["common/tags/a.txt", "common/tags/z.txt"]
    assert index.files_for_tag("USA_du") == ["common/tags/a.txt"]
    assert index.ids_for_country_tag("USA_du") == ["USA_du_test", "nested_file_only"]

    expected_records = _scan_country_records(index, "USA")
    assert index.ids_for_country_tag("usa") == expected_records[0]
    assert _indexed_country_records(index, "USA") == expected_records

    scan_index = _ScanTagIndex(root, cache_dir, include_vanilla=False)
    assert scan_index.ids_for_tag("USA") == expected_ids
    assert scan_index.files_for_tag("USA") == index.files_for_tag("USA")
    assert scan_index.ids_for_country_tag("USA") == expected_records[0]

    file_only_index = _FileOnlyTagIndex(root, cache_dir, include_vanilla=False)
    assert file_only_index.files_for_tag("USA") == index.files_for_tag("USA")

    source_only_index = _SourceOnlyTagIndex(root, cache_dir, include_vanilla=False)
    assert source_only_index.ids_for_country_tag("USA") == expected_records[0]

    tag_only_index = _TagOnlyIndex(root, cache_dir, include_vanilla=False)
    assert tag_only_index.ids_for_country_tag("USA") == expected_records[0]
    assert "exact_file_only" in tag_only_index.ids_for_country_tag("USA")
    assert _indexed_country_records(tag_only_index, "USA") == expected_records


def test_tag_maps_follow_incremental_update_remove_and_cache_reload(tmp_path, cache_dir):
    root = tmp_path / "Mod"
    first = _write(root, "common/tags/a.txt", ["USA_dup", "UsA_case"])
    winner = _write(root, "common/tags/z.txt", ["USA_dup", "usa_case2"])
    topic = _write(root, "common/tags/USA_topics.txt", ["file_only"])
    exact = _write(root, "common/tags/USA.txt", ["exact_file_only"])
    index = _TagIndex(root, cache_dir, include_vanilla=False)

    duplicate = index.resolve("USA_dup")
    assert duplicate is not None
    assert duplicate["file"] == "common/tags/z.txt"
    assert "common/tags/z.txt" in index.files_for_tag("USA")

    winner.unlink()
    topic.write_text("new_file_only\n", encoding="utf-8")
    exact.unlink()
    _write(root, "common/tags/USA_added.txt", ["USA_new", "added_file_only"])
    index._stale_check.force_next()

    assert index.ids_for_tag("USA") == _scan_ids(index, "USA")
    duplicate = index.resolve("USA_dup")
    assert duplicate is not None
    assert duplicate["file"] == str(first.relative_to(root))
    assert "usa_case2" not in index.ids_for_tag("USA")
    assert "exact_file_only" not in index.ids_for_country_tag("USA")
    assert _indexed_country_records(index, "USA") == _scan_country_records(index, "USA")

    reloaded = _TagIndex(root, cache_dir, include_vanilla=False)
    assert reloaded.ids_for_tag("USA") == _scan_ids(reloaded, "USA")
    assert reloaded.files_for_tag("USA") == index.files_for_tag("USA")
    assert reloaded.ids_for_country_tag("USA") == index.ids_for_country_tag("USA")
