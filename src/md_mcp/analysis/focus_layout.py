"""Focus tree geometry — absolute positions, collisions, relative-position chains.

Focus x/y coordinates are grid positions, optionally relative to another focus
via `relative_position_id` (chains allowed). Nothing else in the toolchain can
see the grid, so overlaps and broken chains only surface in-game. This module
resolves every focus to absolute coordinates and reports the problems.

Scope is a tag (its prefix-matched focuses, discovered like `focus_graph`) or a
single mod-relative file (every focus in it). Relative references that point
outside the scope are resolved from the parsed files when possible; references
that resolve nowhere are chain errors.

Ignored in v1: dynamic `offset = { ... }` blocks (trigger-conditional shifts).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Protocol, Set, Tuple

from ..paradox import parse_string
from ..paradox.schema import extract_focus_records
from ..util.encoding import read_text
from ..util.pathing import resolve_scope_file
from ..util.response import coerce_int, enforce_budget, paginate


class _SupportsFilesForTag(Protocol):
    """The only slice of FocusIndex a tag-scoped layout needs."""

    def files_for_tag(self, tag: str) -> List[str]: ...


def focus_layout(
    mod_root: Path,
    focus_index: Optional[_SupportsFilesForTag] = None,
    *,
    tag: Optional[str] = None,
    file: Optional[str] = None,
    vanilla_path: Optional[Path] = None,
    include_positions: bool = False,
    limit: int | float | str | None = 300,
) -> dict:
    """Resolve the focus grid for a tag or file.

    Returns collisions (two+ distinct focuses at the same resolved cell), chain
    errors (missing/cyclic `relative_position_id`, missing x/y), and the
    bounding box. `include_positions=True` adds the per-focus resolved
    coordinates. `limit` caps each of the three lists independently; it
    accepts int, numeric str/float, or None (falls back to 300). An id
    defined in more than one scope file is reported once, under
    `duplicate_definitions`.
    """
    if not tag and not file:
        return {"ok": False, "error": "Pass tag= or file= (mod-relative focus file path)."}
    try:
        limit = coerce_int(limit, name="limit", default=300)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    prefix = ""
    if file:
        candidate_files = [file]
        scope_desc = {"file": file}
    else:
        assert tag is not None
        if focus_index is None:
            return {"ok": False, "error": "tag= scope requires the focus index."}
        prefix = tag.upper() + "_"
        candidate_files = focus_index.files_for_tag(tag)
        scope_desc = {"tag": tag.upper()}

    # Parse every candidate file fully. `all_records` is the resolution set
    # (relative refs may target focuses outside the requested scope);
    # `scope_ids` is what we report on.
    all_records: Dict[str, dict] = {}
    scope_ids: List[str] = []
    seen_in_scope: Set[str] = set()
    duplicate_files: Dict[str, List[str]] = {}
    parse_errors: List[dict] = []
    for relpath in candidate_files:
        abs_path = resolve_scope_file(relpath, mod_root, vanilla_path)
        if abs_path is None:
            parse_errors.append({"file": relpath, "error": "not found"})
            continue
        try:
            text = read_text(abs_path)
            root = parse_string(text)
        except Exception as e:
            parse_errors.append({"file": relpath, "error": str(e)[:200]})
            continue
        for rec in extract_focus_records(root, source=text):
            rec["file"] = relpath
            kept = all_records.setdefault(rec["id"], rec)
            if kept is not rec:
                # Same id defined twice. Keep the first record (as before) but
                # record the clash — without this the id would enter scope_ids
                # twice and collide with itself.
                duplicate_files.setdefault(rec["id"], [kept["file"]]).append(relpath)
            if (file or rec["id"].upper().startswith(prefix)) and rec["id"] not in seen_in_scope:
                seen_in_scope.add(rec["id"])
                scope_ids.append(rec["id"])

    resolved: Dict[str, Tuple[int, int]] = {}
    chain_errors: List[dict] = []
    # Focuses already reported as unresolvable. Without this a broken parent is
    # re-reported once per descendant that walks through it.
    failed: Set[str] = set()

    def _abs_pos(fid: str, visiting: tuple) -> Optional[Tuple[int, int]]:
        if fid in resolved:
            return resolved[fid]
        if fid in failed:
            return None
        rec = all_records.get(fid)
        if rec is None:
            return None
        x, y = _as_int(rec.get("x")), _as_int(rec.get("y"))
        if x is None or y is None:
            chain_errors.append({"focus": fid, "error": "missing_xy", "file": rec["file"]})
            failed.add(fid)
            return None
        rel = rec.get("relative_position_id")
        if not rel:
            resolved[fid] = (x, y)
            return resolved[fid]
        if rel in visiting:
            chain_errors.append(
                {"focus": fid, "error": "cyclic_relative", "ref": rel, "file": rec["file"]}
            )
            failed.add(fid)
            return None
        parent = _abs_pos(rel, (*visiting, fid))
        if parent is None:
            if rel not in all_records:
                # Distinct per referrer: each referring focus has its own broken link.
                chain_errors.append(
                    {"focus": fid, "error": "missing_relative", "ref": rel, "file": rec["file"]}
                )
            # cyclic/missing_xy already reported on the parent itself
            failed.add(fid)
            return None
        resolved[fid] = (parent[0] + x, parent[1] + y)
        return resolved[fid]

    for fid in scope_ids:
        _abs_pos(fid, ())

    # Collisions among scope focuses that resolved.
    by_cell: Dict[Tuple[int, int], List[str]] = {}
    for fid in scope_ids:
        pos = resolved.get(fid)
        if pos is not None:
            by_cell.setdefault(pos, []).append(fid)
    collisions = [
        {"x": x, "y": y, "focuses": sorted(ids)}
        for (x, y), ids in sorted(by_cell.items())
        if len(ids) > 1
    ]

    points = [resolved[f] for f in scope_ids if f in resolved]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    bbox = {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)} if xs else None

    collisions_page, collisions_truncated, collisions_total = paginate(collisions, 0, limit)
    errors_page, errors_truncated, errors_total = paginate(chain_errors, 0, limit)

    result: dict = {
        "ok": True,
        "scope": scope_desc,
        "files_scanned": len(candidate_files),
        "focus_count": len(scope_ids),
        "resolved_count": len(points),
        "collisions_total": collisions_total,
        "collisions": collisions_page,
        "collisions_truncated": collisions_truncated,
        "chain_errors_total": errors_total,
        "chain_errors": errors_page,
        "chain_errors_truncated": errors_truncated,
        "bounding_box": bbox,
    }
    if parse_errors:
        result["parse_errors"] = parse_errors
    if duplicate_files:
        result["duplicate_definitions"] = [
            {"id": fid, "files": files} for fid, files in sorted(duplicate_files.items())
        ]
    if include_positions:
        positions = [
            {
                "id": fid,
                "x": resolved[fid][0],
                "y": resolved[fid][1],
                "relative_to": all_records[fid].get("relative_position_id"),
            }
            for fid in scope_ids
            if fid in resolved
        ]
        positions_page, positions_truncated, positions_total = paginate(positions, 0, limit)
        result["positions_total"] = positions_total
        result["positions"] = positions_page
        result["positions_truncated"] = positions_truncated

    return enforce_budget(
        result,
        heavy_keys=(
            "positions",
            "collisions",
            "chain_errors",
            "parse_errors",
            "duplicate_definitions",
        ),
    )


def _as_int(v) -> Optional[int]:
    if isinstance(v, bool):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
