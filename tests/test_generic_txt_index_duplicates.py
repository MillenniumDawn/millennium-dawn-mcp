"""Duplicate-id surfacing in GenericTxtIndex._rebuild (issue #58)."""

from __future__ import annotations

import logging
from pathlib import Path

from md_mcp.indexes.base import GenericTxtIndex
from md_mcp.util.encoding import read_text


def _parse_ids(abs_path: str, relpath: str) -> list[dict]:
    """One record per non-empty line; the line text is the id."""
    text = read_text(abs_path)
    return [{"id": line.strip()} for line in text.splitlines() if line.strip()]


class _IdIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "test_ids"
    subdir = "common/ids"
    parser_fn = staticmethod(_parse_ids)


def _write(root: Path, name: str, *ids: str) -> None:
    d = root / "common" / "ids"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(ids) + "\n", encoding="utf-8")


def test_duplicate_id_across_two_files_is_logged_and_surfaced(tmp_path, caplog):
    mod = tmp_path / "mod"
    _write(mod, "a.txt", "UNIQUE_A", "DUP_ID")
    _write(mod, "b.txt", "DUP_ID", "UNIQUE_B")
    cache = tmp_path / "cache"

    idx = _IdIndex(mod, cache, include_vanilla=False)
    with caplog.at_level(logging.WARNING):
        idx.ensure_fresh()

    dups = idx.duplicates()
    assert "DUP_ID" in dups
    # Both files are recorded, once each, in sorted order.
    assert [Path(f).name for f in dups["DUP_ID"]] == ["a.txt", "b.txt"]
    assert "UNIQUE_A" not in dups
    assert "UNIQUE_B" not in dups

    # The warning names both files and fires once for the conflict.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "DUP_ID" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "a.txt" in msg and "b.txt" in msg


def test_no_duplicates_means_empty_map(tmp_path):
    mod = tmp_path / "mod"
    _write(mod, "a.txt", "ONE")
    _write(mod, "b.txt", "TWO")
    idx = _IdIndex(mod, tmp_path / "cache", include_vanilla=False)
    idx.ensure_fresh()
    assert idx.duplicates() == {}
    assert idx.resolve("ONE") is not None
    assert idx.resolve("TWO") is not None


def test_duplicate_resolution_is_deterministic(tmp_path):
    mod = tmp_path / "mod"
    _write(mod, "a.txt", "DUP_ID")
    _write(mod, "b.txt", "DUP_ID")
    idx = _IdIndex(mod, tmp_path / "cache", include_vanilla=False)
    idx.ensure_fresh()
    rec = idx.resolve("DUP_ID")
    assert rec is not None
    # Last file in sorted order wins, and it matches the last entry of duplicates().
    assert Path(rec["file"]).name == "b.txt"
    assert Path(idx.duplicates()["DUP_ID"][-1]).name == "b.txt"
