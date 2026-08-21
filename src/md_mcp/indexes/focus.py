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
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..paradox import parse_string
from ..paradox.schema import extract_focus_records, is_focus_file_content
from ..util.encoding import read_text
from .base import (
    IndexCache,
    StaleCheck,
    _default_workers,
    _parser_dispatch,
    _safe_process_context,
    compute_staleness,
    signatures_for,
)

logger = logging.getLogger(__name__)

FOCUS_CACHE_VERSION = 2
FOCUS_SUBDIR = "common/national_focus"


@dataclass(frozen=True)
class FocusParseResult:
    records: Optional[List[dict]]
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
        self._by_file: Dict[str, List[dict]] = {}
        self._by_id: Dict[str, dict] = {}  # focus_id → {file, line, kind, id}
        self._parse_errors: Dict[str, str] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def resolve(self, focus_id: str) -> Optional[dict]:
        self.ensure_fresh()
        return self._by_id.get(focus_id)

    def list_files(self) -> List[str]:
        self.ensure_fresh()
        return sorted(self._by_file.keys())

    def list_keys(self) -> List[str]:
        """Return every focus ID. Named `list_keys` to match the GenericTxtIndex interface."""
        self.ensure_fresh()
        return sorted(self._by_id.keys())

    # Backwards-compatible alias for callers that pre-date the M2 harmonisation.
    list_ids = list_keys

    def files_for_tag(self, tag: str) -> List[str]:
        """Sorted set of files defining a focus whose id starts with `<TAG>_`."""
        self.ensure_fresh()
        prefix = tag.upper() + "_"
        return sorted(
            {r["file"] for fid, r in self._by_id.items() if fid.upper().startswith(prefix)}
        )

    def records_for_file(self, relative_path: str) -> List[dict]:
        self.ensure_fresh()
        return self._by_file.get(relative_path, [])

    def parse_errors(self) -> List[dict]:
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

    def _collect_files(self) -> List[Path]:
        results: List[Path] = []
        for base in self._roots():
            focus_dir = base / FOCUS_SUBDIR
            if focus_dir.is_dir():
                results.extend(p for p in focus_dir.rglob("*.txt") if p.is_file())
        return results

    def _roots(self) -> List[Path]:
        roots = [self.mod_root]
        if self.vanilla_path is not None:
            roots.append(self.vanilla_path)
        return roots

    def _resolve_root(self, relative: str) -> Optional[Path]:
        for base in self._roots():
            candidate = base / relative
            if candidate.exists():
                return base
        return None

    def _rebuild_incremental(self) -> None:
        files = self._collect_files()
        current_sigs = signatures_for(files, self._roots())
        manifest = self._cache.load_manifest() or {}
        staleness = compute_staleness(manifest, current_sigs)

        if self._loaded and not staleness.stale and not staleness.added and not staleness.removed:
            return

        if self._loaded:
            cached_files: Dict[str, List[dict]] = self._by_file
            cached_errors = self._parse_errors
        else:
            cached_data = self._cache.load_data() or {}
            cached_files = cached_data.get("files", {})
            cached_errors = cached_data.get("parse_errors", {})

        new_by_file: Dict[str, List[dict]] = {}
        new_parse_errors: Dict[str, str] = {}
        for relpath in staleness.unchanged:
            if relpath in cached_errors:
                new_parse_errors[relpath] = cached_errors[relpath]
            elif relpath in cached_files:
                new_by_file[relpath] = cached_files[relpath]

        files_to_parse = staleness.stale + staleness.added
        if files_to_parse:
            results = self._parse_parallel(files_to_parse)
            for relpath, parsed in zip(files_to_parse, results, strict=False):
                if parsed is None:
                    new_parse_errors[relpath] = "parser worker failed"
                elif parsed.records is not None:
                    new_by_file[relpath] = parsed.records
                else:
                    new_parse_errors[relpath] = parsed.error or "parse failed"

        new_by_id: Dict[str, dict] = {}
        for relpath, records in new_by_file.items():
            for rec in records:
                new_by_id[rec["id"]] = {**rec, "file": relpath}

        self._by_file = new_by_file
        self._by_id = new_by_id
        self._parse_errors = new_parse_errors

        if files_to_parse or staleness.removed or not manifest:
            self._cache.save_data({"files": new_by_file, "parse_errors": new_parse_errors})
            self._cache.save_manifest(current_sigs)

    def _parse_parallel(self, relpaths: List[str]) -> List[Optional[FocusParseResult]]:
        import os

        jobs: List[tuple] = []
        for rp in relpaths:
            base = self._resolve_root(rp)
            if base is None:
                jobs.append(("", rp))
            else:
                jobs.append((str(base / rp), rp))

        serial = os.environ.get("MD_MCP_SERIAL_PARSE") == "1" or len(jobs) < 4
        if serial:
            return [
                _parse_focus_file(abs_path, rp)
                if abs_path
                else FocusParseResult(None, "file not found")
                for abs_path, rp in jobs
            ]

        ctx = _safe_process_context()
        workers = min(_default_workers(), len(jobs))
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            return list(
                pool.map(_parser_dispatch, [(_parse_focus_file, *job) for job in jobs], chunksize=4)
            )
