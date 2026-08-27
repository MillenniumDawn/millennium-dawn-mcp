"""Focus index — focus_id ↔ file lookup.

Mirrors `MD-VSCode-Utility-Tool/src/util/sharedFocusIndex.ts`. Stores enough metadata
(file path, line number, kind) for `resolve_focus` to navigate directly; the full
parsed record is fetched on-demand to keep cache size manageable.

Cold path parses in a ProcessPoolExecutor so the parser can use all cores; warm
path remains in-process (single-stat refresh + dict diff).

Cache layout (JSON, under <cache_dir>/v2/focus.data.json):
    {
        "files": {
            "<relative_path>": [
                {"id": "ISR_idf_modernization", "line": 42, "kind": "focus_tree"},
                ...
            ]
        },
        "parse_errors": {"<relative_path>": "parse failed: ..."}
    }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..paradox import parse_string
from ..paradox.schema import extract_focus_records, is_focus_file_content
from ..util.encoding import read_text
from .base import (
    IndexCache,
    StaleCheck,
    collect_files,
    parse_files,
    prepare_rebuild,
    roots_for,
)

logger = logging.getLogger(__name__)

FOCUS_CACHE_VERSION = 2
FOCUS_SUBDIR = "common/national_focus"


@dataclass(frozen=True)
class FocusParseResult:
    records: Optional[list[dict]]
    error: Optional[str] = None


def _error_message(prefix: str, exc: Exception) -> str:
    return f"{prefix}: {exc}"[:200]


def _parse_focus_file(abs_path: str, relpath: str) -> FocusParseResult:
    """Module-level parser fn so ProcessPoolExecutor can pickle/dispatch it."""
    try:
        text = read_text(abs_path)
    except OSError as exc:
        logger.warning("focus index: cannot read %s: %s", abs_path, exc)
        return FocusParseResult(None, _error_message("read failed", exc))
    if not is_focus_file_content(text):
        return FocusParseResult([])
    try:
        root = parse_string(text, error_prefix=f"In file {relpath}:\n")
        records = extract_focus_records(root, source=text)
    except Exception as exc:
        logger.warning("focus index: parse failed for %s: %s", abs_path, exc)
        return FocusParseResult(None, _error_message("parse failed", exc))
    return FocusParseResult(
        [{"id": r["id"], "line": r["line"], "kind": r["kind"]} for r in records]
    )


class FocusIndex:
    """Lazy-built, mtime+size-invalidated focus index.

    Public surface:
        * `resolve(focus_id)` → {id, file, line, kind} | None
        * `list_files()` → [relative_path, ...]
        * `list_keys()` / `list_ids()` → [focus_id, ...]
        * `records_for_file(file)` → [{id, line, kind}, ...]
        * `parse_errors()` → [{file, error}, ...]
        * `ensure_fresh()` → re-stat and re-parse any modified files
    """

    def __init__(self, mod_root: Path, cache_dir: Path, vanilla_path: Optional[Path] = None):
        self.mod_root = mod_root
        self.vanilla_path = vanilla_path
        self._cache = IndexCache(cache_dir, "focus", FOCUS_CACHE_VERSION)
        self._stale_check = StaleCheck()

        # In-process state, populated on first ensure_fresh().
        self._by_file: dict[str, list[dict]] = {}
        self._by_id: dict[str, dict] = {}  # focus_id → {file, line, kind, id}
        self._parse_errors: dict[str, str] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def resolve(self, focus_id: str) -> Optional[dict]:
        self.ensure_fresh()
        return self._by_id.get(focus_id)

    def list_files(self) -> list[str]:
        self.ensure_fresh()
        return sorted(self._by_file.keys())

    def list_keys(self) -> list[str]:
        """Return every focus ID. Named `list_keys` to match the GenericTxtIndex interface."""
        self.ensure_fresh()
        return sorted(self._by_id.keys())

    # Backwards-compatible alias for callers that pre-date the M2 harmonisation.
    list_ids = list_keys

    def files_for_tag(self, tag: str) -> list[str]:
        """Sorted set of files defining a focus whose id starts with `<TAG>_`."""
        self.ensure_fresh()
        prefix = tag.upper() + "_"
        return sorted(
            {r["file"] for fid, r in self._by_id.items() if fid.upper().startswith(prefix)}
        )

    def records_for_file(self, relative_path: str) -> list[dict]:
        self.ensure_fresh()
        return self._by_file.get(relative_path, [])

    def parse_errors(self) -> list[dict]:
        self.ensure_fresh()
        return [
            {"file": relpath, "error": error}
            for relpath, error in sorted(self._parse_errors.items())
        ]

    def ensure_fresh(self) -> None:
        if self._loaded and not self._stale_check.should_check():
            return
        self._rebuild_incremental()
        self._loaded = True

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _collect_files(self) -> list[Path]:
        return collect_files(self._roots(), FOCUS_SUBDIR, "*.txt")

    def _roots(self) -> list[Path]:
        return roots_for(self.mod_root, self.vanilla_path)

    def _rebuild_incremental(self) -> None:
        state = prepare_rebuild(
            self._cache,
            self._collect_files(),
            self._roots(),
            self._loaded,
            files=self._by_file,
            parse_errors=self._parse_errors,
        )
        if state is None:
            return
        plan = state.plan

        cached_errors = state.data.get("parse_errors", {})
        new_by_file: dict[str, list[dict]] = state.reused_files()
        new_parse_errors: dict[str, str] = {
            relpath: cached_errors[relpath]
            for relpath in plan.staleness.unchanged
            if relpath in cached_errors
        }
        for relpath in new_parse_errors:
            new_by_file.pop(relpath, None)

        if plan.to_parse:
            results = self._parse_parallel(plan.to_parse)
            for relpath, parsed in zip(plan.to_parse, results, strict=False):
                if parsed is None:
                    new_parse_errors[relpath] = "parser worker failed"
                elif parsed.records is not None:
                    new_by_file[relpath] = parsed.records
                else:
                    new_parse_errors[relpath] = parsed.error or "parse failed"

        new_by_id: dict[str, dict] = {}
        for relpath, records in new_by_file.items():
            for rec in records:
                new_by_id[rec["id"]] = {**rec, "file": relpath}

        self._by_file = new_by_file
        self._by_id = new_by_id
        self._parse_errors = new_parse_errors

        if plan.should_save:
            self._cache.save_data({"files": new_by_file, "parse_errors": new_parse_errors})
            self._cache.save_manifest(plan.current_sigs)

    def _parse_parallel(self, relpaths: list[str]) -> list[Optional[FocusParseResult]]:
        return parse_files(
            _parse_focus_file,
            self._roots(),
            relpaths,
            missing=FocusParseResult(None, "file not found"),
        )
