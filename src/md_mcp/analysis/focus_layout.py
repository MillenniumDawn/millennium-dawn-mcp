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
from typing import Dict, List, Optional, Tuple

from ..indexes import FocusIndex
from ..paradox import parse_string
from ..paradox.schema import extract_focus_records
from ..util.encoding import read_text
from ..util.response import enforce_budget


def focus_layout(
    mod_root: Path,
    focus_index: Optional[FocusIndex] = None,
    *,
    tag: Optional[str] = None,
    file: Optional[str] = None,
    vanilla_path: Optional[Path] = None,
    include_positions: bool = False,
    limit: int = 300,
) -> dict:
    """Resolve the focus grid for a tag or file.

    Returns collisions (two+ focuses at the same resolved cell), chain errors
    (missing/cyclic `relative_position_id`, missing x/y), and the bounding box.
    `include_positions=True` adds the per-focus resolved coordinates (capped by
    `limit`).
    """
    if not tag and not file:
        return {"ok": False, "error": "Pass tag= or file= (mod-relative focus file path)."}

    prefix = ""
    if file:
        candidate_files = [file]
        scope_desc = {"file": file}
    else:
        assert tag is not None
        if focus_index is None:
            return {"ok": False, "error": "tag= scope requires the focus index."}
        focus_index.ensure_fresh()
        prefix = tag.upper() + "_"
        candidate_files = sorted(
            {
                rec["file"]
                for fid in focus_index.list_keys()
                if fid.upper().startswith(prefix) and (rec := focus_index.resolve(fid)) is not None
            }
        )
        scope_desc = {"tag": tag.upper()}

    # Parse every candidate file fully. `all_records` is the resolution set
    # (relative refs may target focuses outside the requested scope);
    # `scope_ids` is what we report on.
    all_records: Dict[str, dict] = {}
    scope_ids: List[str] = []
    parse_errors: List[dict] = []
    for relpath in candidate_files:
        abs_path = _resolve_path(relpath, mod_root, vanilla_path)
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
            all_records.setdefault(rec["id"], rec)
            if file or rec["id"].upper().startswith(prefix):
                scope_ids.append(rec["id"])

    resolved: Dict[str, Tuple[int, int]] = {}
    chain_errors: List[dict] = []

    def _abs_pos(fid: str, visiting: tuple) -> Optional[Tuple[int, int]]:
        if fid in resolved:
            return resolved[fid]
        rec = all_records.get(fid)
        if rec is None:
            return None
        x, y = _as_int(rec.get("x")), _as_int(rec.get("y"))
        if x is None or y is None:
            chain_errors.append({"focus": fid, "error": "missing_xy", "file": rec["file"]})
            return None
        rel = rec.get("relative_position_id")
        if not rel:
            resolved[fid] = (x, y)
            return resolved[fid]
        if rel in visiting:
            chain_errors.append(
                {"focus": fid, "error": "cyclic_relative", "ref": rel, "file": rec["file"]}
            )
            return None
        parent = _abs_pos(rel, (*visiting, fid))
        if parent is None:
            if rel not in all_records:
                chain_errors.append(
                    {"focus": fid, "error": "missing_relative", "ref": rel, "file": rec["file"]}
                )
            # cyclic/missing_xy already reported on the parent itself
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

    xs = [p[0] for f in scope_ids if (p := resolved.get(f))]
    ys = [p[1] for f in scope_ids if (p := resolved.get(f))]
    bbox = {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)} if xs else None

    result: dict = {
        "ok": True,
        "scope": scope_desc,
        "files_scanned": len(candidate_files),
        "focus_count": len(scope_ids),
        "resolved_count": sum(1 for f in scope_ids if f in resolved),
        "collision_count": len(collisions),
        "collisions": collisions,
        "chain_errors": chain_errors,
        "bounding_box": bbox,
    }
    if parse_errors:
        result["parse_errors"] = parse_errors
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
        result["positions_total"] = len(positions)
        result["positions"] = positions[:limit]
        result["positions_truncated"] = len(positions) > limit

    return enforce_budget(result, heavy_keys=("positions", "collisions", "chain_errors"))


def _as_int(v) -> Optional[int]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _resolve_path(relpath: str, mod_root: Path, vanilla_path: Optional[Path]) -> Optional[Path]:
    p = mod_root / relpath
    if p.exists():
        return p
    if vanilla_path is not None:
        p = vanilla_path / relpath
        if p.exists():
            return p
    return None
