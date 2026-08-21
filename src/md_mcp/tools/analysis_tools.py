"""Analysis tools — structural queries over the indexes."""

from __future__ import annotations

from typing import List, Optional

from ..config import Settings
from ..indexes import FocusIndex
from ..paradox import parse_string
from ..paradox.schema import extract_focus_records
from ..util.encoding import read_text
from ..util.pathing import resolve_scope_file
from ..util.response import enforce_budget

_MAX_PARTIAL_ERRORS = 20
_MAX_MISSING_RECORD_IDS = 5
_MAX_ERROR_CHARS = 200


def find_focuses_tool(
    settings: Settings,
    focus_index: FocusIndex,
    *,
    tag: Optional[str] = None,
    has_prereq: Optional[str] = None,
    mutex_with: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 200,
) -> dict:
    """Predicate search over the focus index.

    Filters (all optional, AND-combined):
      * `tag`         — focus ID starts with `<tag>_` (case-insensitive). E.g. `tag=ISR`
                        matches `ISR_idf_modernization`, `isr_*`, etc.
      * `has_prereq`  — focus lists this id in any `prerequisite` group
      * `mutex_with`  — focus lists this id in its `mutually_exclusive` block
      * `kind`        — restrict to `focus_tree`, `shared_focus`, or `joint_focus`

    Returns up to `limit` matches with file, line, and kind. For deep-detail filters
    (`has_prereq`, `mutex_with`), this re-parses the candidate files — sublinear in
    practice because tag/kind filters prune first.
    """
    focus_index.ensure_fresh()
    index_errors = focus_index.parse_errors()
    matches: List[dict] = []

    # Cheap filters first.
    candidates: List[dict] = []
    for fid, rec in _all_records(focus_index):
        if tag is not None and not fid.lower().startswith(tag.lower() + "_"):
            continue
        if kind is not None and rec["kind"] != kind:
            continue
        candidates.append({"id": fid, **rec})

    partial_errors = list(index_errors)
    skipped_files = {error["file"] for error in index_errors}
    skipped_records = 0

    if has_prereq or mutex_with:
        candidates, deep_errors, deep_skipped_files, skipped_records = _filter_deep(
            candidates, settings, has_prereq, mutex_with
        )
        partial_errors.extend(deep_errors)
        skipped_files.update(deep_skipped_files)

    for rec in candidates[:limit]:
        matches.append(
            {"id": rec["id"], "file": rec["file"], "line": rec["line"], "kind": rec["kind"]}
        )

    partial_errors_total = len(partial_errors)
    partial_errors = partial_errors[:_MAX_PARTIAL_ERRORS]
    result = {
        "ok": True,
        "partial": bool(skipped_files or skipped_records),
        "skipped_files": len(skipped_files),
        "skipped_records": skipped_records,
        "partial_errors_total": partial_errors_total,
        "partial_errors": partial_errors,
        "partial_errors_truncated": partial_errors_total > len(partial_errors),
        "total": len(candidates),
        "count": len(matches),
        "truncated": len(candidates) > limit,
        "matches": matches,
    }
    return enforce_budget(result, heavy_keys=("matches", "partial_errors"))


def _all_records(focus_index: FocusIndex):
    for relpath in focus_index.list_files():
        for rec in focus_index.records_for_file(relpath):
            yield rec["id"], {**rec, "file": relpath}


def _filter_deep(
    candidates: List[dict],
    settings: Settings,
    has_prereq: Optional[str],
    mutex_with: Optional[str],
) -> tuple[List[dict], List[dict], set[str], int]:
    if not candidates:
        return [], [], set(), 0

    by_file: dict[str, List[dict]] = {}
    for c in candidates:
        by_file.setdefault(c["file"], []).append(c)

    keep: List[dict] = []
    errors: List[dict] = []
    skipped_files: set[str] = set()
    skipped_records = 0
    for relpath, group in by_file.items():
        abs_path = resolve_scope_file(relpath, settings.mod_root, settings.vanilla_path)
        if abs_path is None:
            errors.append({"file": relpath, "error": "not found"})
            skipped_files.add(relpath)
            continue
        try:
            text = read_text(abs_path)
            root = parse_string(text)
        except Exception as exc:
            errors.append({"file": relpath, "error": str(exc)[:_MAX_ERROR_CHARS]})
            skipped_files.add(relpath)
            continue
        details = {r["id"]: r for r in extract_focus_records(root, source=text)}
        missing_ids = [c["id"] for c in group if c["id"] not in details]
        if missing_ids:
            skipped_records += len(missing_ids)
            errors.append(
                {
                    "file": relpath,
                    "error": "indexed records absent from reparsed AST",
                    "count": len(missing_ids),
                    "sample_ids": missing_ids[:_MAX_MISSING_RECORD_IDS],
                    "sample_ids_truncated": len(missing_ids) > _MAX_MISSING_RECORD_IDS,
                }
            )

        for c in group:
            detail = details.get(c["id"])
            if detail is None:
                continue
            if has_prereq:
                flat_prereqs = {p for group_ in detail["prerequisites"] for p in group_}
                if has_prereq not in flat_prereqs:
                    continue
            if mutex_with and mutex_with not in detail["mutually_exclusive"]:
                continue
            keep.append(c)
    return keep, errors, skipped_files, skipped_records
