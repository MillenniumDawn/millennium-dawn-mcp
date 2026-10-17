"""Focus tree geometry — tree-scoped absolute positions and collisions.

The resolver follows the static assembly and iterative position rules used by
Millennium Dawn's focus geometry validator, while keeping this tool's compact
projection and pagination contract.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional, Protocol

from ..paradox.nodes import Node, SymbolNode, node_line, symbol_or_str
from ..util.line_numbers import line_starts
from ..util.pathing import roots_for
from ..util.response import coerce_int, enforce_budget, paginate
from .scope import iter_scope_files


class _SupportsFilesForTag(Protocol):
    """The only required slice of FocusIndex for tag-scoped layouts."""

    # Protocol method declaration — `...` is the abstract body, not a stub.
    # pi-lens-ignore: no-ellipsis-body
    def files_for_tag(self, tag: str) -> list[str]: ...


def _available_shared_files(
    mod_root: Path,
    vanilla_path: Path | None,
    submod_root: Path | None,
    focus_index: Optional[_SupportsFilesForTag],
) -> list[str]:
    if focus_index is not None:
        list_files = getattr(focus_index, "list_files", None)
        records_for_file = getattr(focus_index, "records_for_file", None)
        if list_files is not None and records_for_file is not None:
            return [
                relpath
                for relpath in list_files()
                if any(
                    record.get("kind") in {"shared_focus", "joint_focus"}
                    for record in records_for_file(relpath)
                )
            ]

    files: list[str] = []
    seen: set[str] = set()
    for root in roots_for(mod_root, vanilla_path, submod_root):
        directory = root / "common" / "national_focus"
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.txt")):
            relpath = path.relative_to(root).as_posix()
            if relpath not in seen and path.is_file():
                seen.add(relpath)
                files.append(relpath)
    return files


def focus_layout(
    mod_root: Path,
    focus_index: Optional[_SupportsFilesForTag] = None,
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
        files.append(_layout_file(parsed.relpath, parsed.root, parsed.text))

    if any(tree["shared_refs"] for source in files for tree in source["trees"]):
        shared_files = _available_shared_files(mod_root, vanilla_path, submod_root, focus_index)
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
            files.append(_layout_file(parsed.relpath, parsed.root, parsed.text))

    shared: dict[str, list[dict]] = defaultdict(list)
    for source in files:
        for focus in source["shared"]:
            shared[focus["id"]].append(focus)
    shared_dependencies = {
        name: {
            parent
            for focus in definitions
            for group in focus["prerequisites"]
            for parent in group
            if parent in shared
        }
        for name, definitions in shared.items()
    }
    shared_dependents: dict[str, set[str]] = defaultdict(set)
    for name, parents in shared_dependencies.items():
        for parent in parents:
            shared_dependents[parent].add(name)

    instances: list[dict] = []
    for source in files:
        for tree in source["trees"]:
            imported = set(tree["shared_refs"]) & shared.keys()
            pending = deque(sorted(imported))
            remaining: dict[str, set[str]] = {}
            while pending:
                name = pending.popleft()
                for child in sorted(shared_dependents.get(name, ())):
                    if child in imported:
                        continue
                    remaining.setdefault(child, shared_dependencies[child] - imported)
                    remaining[child].discard(name)
                    if not remaining[child]:
                        imported.add(child)
                        pending.append(child)
            focuses = [*tree["focuses"]]
            for name in sorted(imported):
                focuses.extend(shared.get(name, ()))
            instances.append(
                {
                    "key": (source["filepath"], tree["id"], tree["line"]),
                    "tree": tree,
                    "focuses": focuses,
                    "missing_shared": sorted(set(tree["shared_refs"]) - shared.keys()),
                }
            )

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
    geometry: list[tuple[dict, dict, set[str], list[dict]]] = []

    for instance in scope_instances:
        focuses = instance["focuses"]
        by_id: dict[str, list[dict]] = defaultdict(list)
        for focus in focuses:
            by_id[focus["id"]].append(focus)
        hidden = _hidden_focus_ids(focuses)
        positions, failures = _resolve_positions(by_id)
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
        geometry.append((instance, positions, hidden, instance["reported"]))
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
            x, y, dynamic_offset = position
            bbox_points.append((x, y))
            resolved_total += 1
            if include_positions:
                output_positions.append(
                    {
                        "id": focus["id"],
                        "x": x,
                        "y": y,
                        "relative_to": focus["relative"],
                        "tree": instance["tree"]["id"],
                        "tree_line": instance["tree"]["line"],
                        "file": focus["file"],
                        "line": focus["line"],
                        "dynamic": focus["id"] in hidden or dynamic_offset,
                    }
                )

    # Keep collisions tree-local: same coordinates in unrelated trees are not clashes.
    collision_by_tree: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for instance, resolved, hidden_ids, reported in geometry:
        tree_key = instance["key"]
        local: dict[tuple[float, float], list[dict]] = defaultdict(list)
        for focus in reported:
            pos = resolved.get(focus["id"])
            if pos is not None and focus["id"] not in hidden_ids and not pos[2]:
                local[(pos[0], pos[1])].append(focus)
        for (x, y), defs in sorted(local.items()):
            if len({(f["id"], f["file"], f["line"]) for f in defs}) > 1:
                collision_by_tree[tree_key].append(
                    {
                        "x": x,
                        "y": y,
                        "focuses": sorted(f["id"] for f in defs),
                        "tree": instance["tree"]["id"],
                        "tree_line": instance["tree"]["line"],
                        "file": instance["key"][0],
                    }
                )
    collisions = [item for values in collision_by_tree.values() for item in values]
    spacing_warnings: list[dict] = []
    for instance, positions, hidden_ids, reported in geometry:
        rows: dict[float, dict[float, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for focus in reported:
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
                            spacing_warnings.append(
                                {
                                    "x": x,
                                    "y": y,
                                    "other_x": other_x,
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


def _layout_file(filepath: str, root: Node, source: str) -> dict:
    starts = line_starts(source)
    trees = []
    shared = []
    for node in root.children():
        if node.name == "focus_tree":
            tree_id = symbol_or_str(node.get("id")) or f"tree at line {node_line(node, starts)}"
            tree: dict[str, Any] = {
                "id": tree_id,
                "line": node_line(node, starts),
                "focuses": [],
                "shared_refs": [],
            }
            for child in node.children():
                if child.name == "focus":
                    record = _focus_layout_record(child, filepath, starts)
                    if record:
                        tree["focuses"].append(record)
                elif child.name == "shared_focus":
                    ref = symbol_or_str(child)
                    if ref:
                        tree["shared_refs"].append(ref)
            trees.append(tree)
        elif node.name in ("shared_focus", "joint_focus"):
            record = _focus_layout_record(node, filepath, starts)
            if record:
                shared.append(record)
    return {"filepath": filepath, "trees": trees, "shared": shared}


def _focus_layout_record(node: Node, filepath: str, starts: list[int]) -> dict | None:
    focus_id = symbol_or_str(node.get("id"))
    if not focus_id:
        return None
    x = _coordinate(node.get("x"))
    y = _coordinate(node.get("y"))
    prerequisites = []
    for block in node.get_all("prerequisite"):
        group = [
            value
            for child in block.children()
            if child.name == "focus" and (value := symbol_or_str(child))
        ]
        if group:
            prerequisites.append(group)
    mutex = []
    mutex_node = node.get("mutually_exclusive")
    if mutex_node:
        mutex = [
            value
            for child in mutex_node.children()
            if child.name == "focus" and (value := symbol_or_str(child))
        ]
    dynamic = node.get("offset") is not None
    return {
        "id": focus_id,
        "file": filepath,
        "line": node_line(node, starts),
        "x": x,
        "y": y,
        "relative": symbol_or_str(node.get("relative_position_id")),
        "allow_branch": node.get("allow_branch") is not None,
        "offset": dynamic,
        "prerequisites": prerequisites,
        "mutually_exclusive": mutex,
    }


def _coordinate(node: Node | None) -> float | None:
    value = None if node is None else node.value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        try:
            result = float(value)
        except OverflowError:
            return None
    elif isinstance(value, float):
        result = value
    elif isinstance(value, SymbolNode):
        try:
            result = float(value.name)
        except ValueError:
            return None
    elif isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _resolve_positions(
    index: dict[str, list[dict]],
) -> tuple[dict[str, tuple[float, float, bool] | None], dict]:
    """Iterative port of the upstream geometry resolver; handles arbitrarily long chains."""
    positions: dict[str, tuple[float, float, bool] | None] = {}
    failures: dict[tuple[str, str], tuple[dict, str]] = {}
    for start in sorted(index):
        path: list[str] = []
        active: set[str] = set()
        current = start
        while current not in positions:
            if current in active:
                cycle = path[path.index(current) :]
                root = min(cycle)
                failures[("cycle", root)] = (index[root][0], "cyclic_relative")
                positions[current] = None
                break
            if current not in index:
                origin = index[path[-1]][0]
                failures[("missing", current)] = (origin, "missing_relative")
                positions[current] = None
                break
            definitions = index[current]
            focus = definitions[0]
            if len(definitions) != 1:
                failures[("ambiguous", current)] = (focus, "duplicate_definition")
                positions[current] = None
                break
            if focus["x"] is None or focus["y"] is None:
                failures[("coordinates", current)] = (focus, "missing_xy")
                positions[current] = None
                break
            path.append(current)
            active.add(current)
            if not focus["relative"]:
                positions[current] = (focus["x"], focus["y"], focus["offset"])
                break
            current = focus["relative"]
        for name in reversed(path):
            if name in positions:
                continue
            focus = index[name][0]
            anchor = positions[focus["relative"]]
            if anchor is None:
                positions[name] = None
                continue
            x = anchor[0] + focus["x"]
            y = anchor[1] + focus["y"]
            if not math.isfinite(x) or not math.isfinite(y):
                positions[name] = None
                failures[("coordinates", name)] = (focus, "nonfinite_xy")
            else:
                positions[name] = (x, y, anchor[2] or focus["offset"])
    return positions, failures


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
