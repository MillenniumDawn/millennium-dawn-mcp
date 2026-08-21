"""Analysis tools — structural queries over the indexes."""

from __future__ import annotations

from typing import List, Optional

from ..config import Settings
from ..indexes import FocusIndex
from ..paradox import parse_string
from ..paradox.schema import extract_focus_records
from ..util.encoding import read_text
from ..util.pathing import resolve_scope_file


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
    matches: List[dict] = []

    # Cheap filters first.
    candidates: List[dict] = []
    for fid, rec in _all_records(focus_index):
        if tag is not None and not fid.lower().startswith(tag.lower() + "_"):
            continue
        if kind is not None and rec["kind"] != kind:
            continue
        candidates.append({"id": fid, **rec})

    # Deep filters require parsing the file.
    if has_prereq or mutex_with:
        candidates = _filter_deep(candidates, settings, has_prereq, mutex_with)

    for rec in candidates[:limit]:
        matches.append(
            {"id": rec["id"], "file": rec["file"], "line": rec["line"], "kind": rec["kind"]}
        )

    return {
        "ok": True,
        "count": len(matches),
        "truncated": len(candidates) > limit,
        "matches": matches,
    }


def _all_records(focus_index: FocusIndex):
    for relpath in focus_index.list_files():
        for rec in focus_index.records_for_file(relpath):
            yield rec["id"], {**rec, "file": relpath}


def _filter_deep(
    candidates: List[dict],
    settings: Settings,
    has_prereq: Optional[str],
    mutex_with: Optional[str],
) -> List[dict]:
    if not candidates:
        return []

    # Group by file so we parse each file at most once.
    by_file: dict[str, List[dict]] = {}
    for c in candidates:
        by_file.setdefault(c["file"], []).append(c)

    keep: List[dict] = []
    for relpath, group in by_file.items():
        abs_path = resolve_scope_file(relpath, settings.mod_root, settings.vanilla_path)
        if abs_path is None:
            continue
        try:
            text = read_text(abs_path)
            root = parse_string(text)
        except Exception:
            continue
        details = {r["id"]: r for r in extract_focus_records(root, source=text)}

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
    return keep
