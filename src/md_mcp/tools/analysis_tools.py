"""Analysis tools — structural queries over the indexes."""

from __future__ import annotations

from typing import Optional

from ..analysis.scope import iter_scope_files
from ..config import Settings
from ..indexes import FocusIndex
from ..paradox.schema import extract_focus_records
from ..util.response import coerce_int, enforce_budget, paginate

_MAX_PARTIAL_ERRORS = 20
_MAX_MISSING_RECORD_IDS = 5


def find_focuses_tool(
    settings: Settings,
    focus_index: FocusIndex,
    *,
    tag: Optional[str] = None,
    has_prereq: Optional[str] = None,
    mutex_with: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int | float | str | None = 200,
    offset: int | float | str | None = 0,
) -> dict:
    """Predicate search over the focus index.

    Filters (all optional, AND-combined):
      * `tag`         — focus ID starts with `<tag>_` (case-insensitive). E.g. `tag=ISR`
                        matches `ISR_idf_modernization`, `isr_*`, etc.
      * `has_prereq`  — focus lists this id in any `prerequisite` group
      * `mutex_with`  — focus lists this id in its `mutually_exclusive` block
      * `kind`        — restrict to `focus_tree`, `shared_focus`, or `joint_focus`

    Returns a paginated match list with file, line, kind, and total/returned/truncated
    metadata. For deep-detail filters (`has_prereq`, `mutex_with`), this re-parses
    candidate files — sublinear in practice because tag/kind filters prune first.
    """
    try:
        limit = coerce_int(limit, name="limit", default=200)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return enforce_budget({"ok": False, "error": str(exc)})

    focus_index.ensure_fresh()
    index_errors = focus_index.parse_errors()
    matches: list[dict] = []

    # Cheap filters first.
    candidates: list[dict] = []
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

    match_page, truncated, total = paginate(candidates, offset=offset, limit=limit)
    for rec in match_page:
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
        "total": total,
        "returned": len(matches),
        "count": len(matches),
        "truncated": truncated,
        "matches": matches,
    }
    return enforce_budget(result, heavy_keys=("matches", "partial_errors"))


def _all_records(focus_index: FocusIndex):
    for relpath in focus_index.list_files():
        for rec in focus_index.records_for_file(relpath):
            yield rec["id"], {**rec, "file": relpath}


def _filter_deep(
    candidates: list[dict],
    settings: Settings,
    has_prereq: Optional[str],
    mutex_with: Optional[str],
) -> tuple[list[dict], list[dict], set[str], int]:
    if not candidates:
        return [], [], set(), 0

    by_file: dict[str, list[dict]] = {}
    for c in candidates:
        by_file.setdefault(c["file"], []).append(c)

    keep: list[dict] = []
    errors: list[dict] = []
    skipped_files: set[str] = set()
    skipped_records = 0
    for parsed in iter_scope_files(
        by_file, settings.mod_root, settings.vanilla_path, errors, skipped_files
    ):
        relpath = parsed.relpath
        group = by_file[relpath]
        details = {r["id"]: r for r in extract_focus_records(parsed.root, source=parsed.text)}
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
