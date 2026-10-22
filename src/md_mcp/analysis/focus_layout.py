"""Focus tree geometry — tree-scoped absolute positions and collisions.

Assembly and positions come from `focus_tree`, which follows the static
assembly and iterative position rules used by Millennium Dawn's focus geometry
validator. This tool adds collisions and spacing warnings, with a compact
projection and pagination contract.
"""

from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..util.response import coerce_int, enforce_budget, paginate
from .focus_tree import (
    SupportsFilesForTag,
    assemble_tree_instances,
    available_shared_files,
    layout_file,
    resolve_positions,
    scope_relpath,
)
from .scope import iter_scope_files


def focus_layout(
    mod_root: Path,
    focus_index: Optional[SupportsFilesForTag] = None,
    *,
    tag: Optional[str] = None,
    file: Optional[str] = None,
    vanilla_path: Optional[Path] = None,
    submod_root: Optional[Path] = None,
    include_positions: bool = False,
    limit: int | float | str | None = 300,
    offset: int | float | str | None = 0,
) -> dict:
    """Resolve tree-scoped focus geometry for a tag or file."""
    if not tag and not file:
        return {"ok": False, "error": "Pass tag= or file= (mod-relative focus file path)."}
    try:
        limit = coerce_int(limit, name="limit", default=300)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if file:
        file = scope_relpath(file, mod_root, vanilla_path, submod_root)
        candidate_files = [file]
        scope_desc = {"file": file}
        prefix = None
    else:
        assert tag is not None
        if focus_index is None:
            return {"ok": False, "error": "tag= scope requires the focus index."}
        prefix = tag.upper() + "_"
        candidate_files = focus_index.files_for_tag(tag)
        scope_desc = {"tag": tag.upper()}

    parse_files = list(dict.fromkeys(candidate_files))
    parse_errors: list[dict] = []
    files: list[dict] = []
    for parsed in iter_scope_files(
        parse_files, mod_root, vanilla_path, parse_errors, submod_root=submod_root
    ):
        files.append(layout_file(parsed.relpath, parsed.root, parsed.text))

    if any(tree["shared_refs"] for source in files for tree in source["trees"]):
        shared_files = available_shared_files(mod_root, vanilla_path, submod_root, focus_index)
        parsed_paths = set(parse_files)
        additional_shared_files = [
            relpath for relpath in shared_files if relpath not in parsed_paths
        ]
        parse_files.extend(additional_shared_files)
        for parsed in iter_scope_files(
            additional_shared_files,
            mod_root,
            vanilla_path,
            parse_errors,
            submod_root=submod_root,
        ):
            files.append(layout_file(parsed.relpath, parsed.root, parsed.text))

    instances = assemble_tree_instances(files)

    scope_instances = [
        instance
        for instance in instances
        if (file and instance["key"][0] == file)
        or (not file and any(f["id"].upper().startswith(prefix or "") for f in instance["focuses"]))
    ]
    for instance in scope_instances:
        instance["reported"] = (
            instance["focuses"]
            if file
            else [
                focus
                for focus in instance["focuses"]
                if focus["id"].upper().startswith(prefix or "")
            ]
        )
    output_positions: list[dict] = []
    resolved_total = 0
    chain_errors: list[dict] = []
    duplicate_contexts: list[dict] = []
    bbox_points: list[tuple[float, float]] = []
    geometry: list[tuple[dict, dict, set[str], set[str]]] = []

    for instance in scope_instances:
        focuses = instance["focuses"]
        by_id: dict[str, list[dict]] = defaultdict(list)
        for focus in focuses:
            by_id[focus["id"]].append(focus)
        hidden = _hidden_focus_ids(focuses)
        positions, failures = resolve_positions(by_id)
        reported_ids = {focus["id"] for focus in instance["reported"]}
        relevant_ids = _relative_ancestors(reported_ids, by_id)
        for focus_id, definitions in sorted(by_id.items()):
            if len(definitions) > 1 and focus_id in relevant_ids:
                duplicate_contexts.append(
                    {
                        "id": focus_id,
                        "tree": instance["tree"]["id"],
                        "tree_line": instance["tree"]["line"],
                        "file": instance["key"][0],
                        "files": sorted(f["file"] for f in definitions),
                    }
                )
        geometry.append((instance, positions, hidden, reported_ids))
        for missing_ref in instance["missing_shared"]:
            chain_errors.append(
                {
                    "focus": missing_ref,
                    "error": "missing_shared",
                    "ref": missing_ref,
                    "file": instance["key"][0],
                    "line": instance["tree"]["line"],
                    "tree": instance["tree"]["id"],
                    "tree_line": instance["tree"]["line"],
                }
            )
        for (_kind, _id), (focus, reason) in sorted(failures.items()):
            if focus["id"] in relevant_ids and reason != "missing_relative":
                chain_errors.append(
                    {
                        "focus": focus["id"],
                        "error": reason,
                        "file": focus["file"],
                        "line": focus["line"],
                        "tree": instance["tree"]["id"],
                        "tree_line": instance["tree"]["line"],
                    }
                )
        for focus in focuses:
            if focus["id"] in relevant_ids and focus["relative"] and focus["relative"] not in by_id:
                chain_errors.append(
                    {
                        "focus": focus["id"],
                        "error": "missing_relative",
                        "ref": focus["relative"],
                        "file": focus["file"],
                        "line": focus["line"],
                        "tree": instance["tree"]["id"],
                        "tree_line": instance["tree"]["line"],
                    }
                )
        for focus in instance["reported"]:
            position = positions.get(focus["id"])
            if position is None:
                continue
            point = (float(position[0]), float(position[1]))
            bbox_points.append(point)
            resolved_total += 1
            if include_positions:
                output_positions.append(
                    {
                        "id": focus["id"],
                        "x": point[0],
                        "y": point[1],
                        "relative_to": focus["relative"],
                        "tree": instance["tree"]["id"],
                        "tree_line": instance["tree"]["line"],
                        "file": focus["file"],
                        "line": focus["line"],
                        "dynamic": focus["id"] in hidden or position[2],
                    }
                )

    # Keep collisions tree-local: same coordinates in unrelated trees are not clashes.
    # Check the whole tree, and report what involves at least one in-scope focus.
    collision_by_tree: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for instance, resolved, hidden_ids, reported_ids in geometry:
        tree_key = instance["key"]
        local: dict[tuple[Decimal, Decimal], list[dict]] = defaultdict(list)
        for focus in instance["focuses"]:
            pos = resolved.get(focus["id"])
            if pos is not None and focus["id"] not in hidden_ids and not pos[2]:
                local[(pos[0], pos[1])].append(focus)
        for (x, y), defs in sorted(local.items()):
            if len({(f["id"], f["file"], f["line"]) for f in defs}) > 1 and any(
                f["id"] in reported_ids for f in defs
            ):
                collision_by_tree[tree_key].append(
                    {
                        "x": float(x),
                        "y": float(y),
                        "focuses": sorted(f["id"] for f in defs),
                        "tree": instance["tree"]["id"],
                        "tree_line": instance["tree"]["line"],
                        "file": instance["key"][0],
                    }
                )
    collisions = [item for values in collision_by_tree.values() for item in values]
    spacing_warnings: list[dict] = []
    for instance, positions, hidden_ids, reported_ids in geometry:
        rows: dict[Decimal, dict[Decimal, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for focus in instance["focuses"]:
            position = positions.get(focus["id"])
            if position is not None and focus["id"] not in hidden_ids and not position[2]:
                rows[position[1]][position[0]].append(focus)
        for y, columns in sorted(rows.items()):
            xs = sorted(columns)
            for i, x in enumerate(xs):
                for j in range(i + 1, len(xs)):
                    other_x = xs[j]
                    if other_x - x >= 2:
                        break
                    for left in columns[x]:
                        for right in columns[other_x]:
                            if left["id"] not in reported_ids and right["id"] not in reported_ids:
                                continue
                            spacing_warnings.append(
                                {
                                    "x": float(x),
                                    "y": float(y),
                                    "other_x": float(other_x),
                                    "focuses": sorted((left["id"], right["id"])),
                                    "tree": instance["tree"]["id"],
                                    "tree_line": instance["tree"]["line"],
                                    "file": instance["key"][0],
                                }
                            )

    bbox = (
        {
            "min_x": min(x for x, _ in bbox_points),
            "max_x": max(x for x, _ in bbox_points),
            "min_y": min(y for _, y in bbox_points),
            "max_y": max(y for _, y in bbox_points),
        }
        if bbox_points
        else None
    )
    collisions_page, collisions_truncated, collisions_total = paginate(collisions, offset, limit)
    spacing_page, spacing_truncated, spacing_total = paginate(spacing_warnings, offset, limit)
    errors_page, errors_truncated, errors_total = paginate(chain_errors, offset, limit)
    result: dict = {
        "ok": True,
        "scope": scope_desc,
        "files_scanned": len(parse_files),
        "focus_count": sum(len(instance["reported"]) for instance in scope_instances),
        "resolved_count": resolved_total,
        "collisions_total": collisions_total,
        "collisions": collisions_page,
        "collisions_truncated": collisions_truncated,
        "spacing_warnings_total": spacing_total,
        "spacing_warnings": spacing_page,
        "spacing_warnings_truncated": spacing_truncated,
        "chain_errors_total": errors_total,
        "chain_errors": errors_page,
        "chain_errors_truncated": errors_truncated,
        "bounding_box": bbox,
    }
    if parse_errors:
        result["parse_errors"] = parse_errors
    if duplicate_contexts:
        duplicates_page, duplicates_truncated, duplicates_total = paginate(
            duplicate_contexts, offset, limit
        )
        result.update(
            duplicate_definitions=duplicates_page,
            duplicate_definitions_total=duplicates_total,
            duplicate_definitions_truncated=duplicates_truncated,
        )
    if include_positions:
        positions_page, positions_truncated, positions_total = paginate(
            output_positions, offset, limit
        )
        result.update(
            positions_total=positions_total,
            positions=positions_page,
            positions_truncated=positions_truncated,
        )
    return enforce_budget(
        result,
        heavy_keys=(
            "positions",
            "spacing_warnings",
            "collisions",
            "chain_errors",
            "parse_errors",
            "duplicate_definitions",
        ),
    )


def _relative_ancestors(focus_ids: set[str], by_id: dict[str, list[dict]]) -> set[str]:
    relevant = set(focus_ids)
    pending = list(focus_ids)
    while pending:
        focus = by_id.get(pending.pop())
        if not focus:
            continue
        relative = focus[0]["relative"]
        if relative and relative not in relevant:
            relevant.add(relative)
            pending.append(relative)
    return relevant


def _hidden_focus_ids(focuses: list[dict]) -> set[str]:
    children: dict[str, set[str]] = defaultdict(set)
    hidden = {focus["id"] for focus in focuses if focus["allow_branch"]}
    for focus in focuses:
        for group in focus["prerequisites"]:
            for parent in group:
                children[parent].add(focus["id"])
    pending = deque(sorted(hidden))
    while pending:
        for child in children[pending.popleft()] - hidden:
            hidden.add(child)
            pending.append(child)
    return hidden
