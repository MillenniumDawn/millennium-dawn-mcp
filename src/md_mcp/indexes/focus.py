"""Focus index — focus_id ↔ file lookup.

Mirrors `MD-VSCode-Utility-Tool/src/util/sharedFocusIndex.ts`. Stores enough metadata
(file path, line number, kind) for `resolve_focus` to navigate directly; the full
parsed record is fetched on-demand to keep cache size manageable.

Cold path parses in a ProcessPoolExecutor so the parser can use all cores; warm
path remains in-process (single-stat refresh + dict diff).

Cache layout (JSON, under <cache_dir>/v3/focus.data.json):
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
from typing import Optional

from ..paradox import parse_string
from ..paradox.schema import extract_focus_records, is_focus_file_content
from ..util.encoding import read_text
from .base import GenericTxtIndex

logger = logging.getLogger(__name__)

FOCUS_CACHE_VERSION = 3
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


class FocusIndex(GenericTxtIndex):
    """Focus ID index with parse-error reporting and tag-based file lookup."""

    cache_version = FOCUS_CACHE_VERSION
    cache_name = "focus"
    subdir = FOCUS_SUBDIR
    parser_fn = staticmethod(_parse_focus_file)
    missing_result = FocusParseResult(None, "file not found")
    track_parse_errors = True

    # Backwards-compatible alias for callers that pre-date the M2 harmonisation.
    list_ids = GenericTxtIndex.list_keys

    def files_for_tag(self, tag: str) -> list[str]:
        """Sorted set of files defining a focus whose id starts with `<TAG>_`."""
        self.ensure_fresh()
        prefix = tag.upper() + "_"
        return sorted(
            {r["file"] for fid, r in self._by_key.items() if fid.upper().startswith(prefix)}
        )
