"""Focus tree assembly shared by the focus tools.

Reads the trees and shared focuses of parsed focus files, assembles each
focus_tree with the shared focuses it imports, and resolves static positions the
way Millennium Dawn's focus geometry validator does. `focus_layout` and
`render_focus_tree` both build on it.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, Protocol

from ..paradox.nodes import Node, SymbolNode, node_line, symbol_or_str
from ..paradox.schema import focus_mutually_exclusive, focus_prerequisites
from ..util.line_numbers import line_starts
from ..util.pathing import contained, roots_for


class SupportsFilesForTag(Protocol):
    """The only required slice of FocusIndex for tag-scoped focus tools."""

    # Protocol method declaration: `...` is the abstract body, not a stub.
    # pi-lens-ignore: no-ellipsis-body
    def files_for_tag(self, tag: str) -> list[str]: ...


def available_shared_files(
    mod_root: Path,
    vanilla_path: Path | None,
    submod_root: Path | None,
    focus_index: Optional[SupportsFilesForTag],
) -> list[str]:
    """Focus files that may define shared or joint focuses.

    The index's shared files when it can tell, otherwise every focus file.
    """
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


def scope_relpath(
    file: str, mod_root: Path, vanilla_path: Path | None, submod_root: Path | None
) -> str:
    """The mod-relative path a caller's `file=` resolves to, or `file` unchanged."""
    # Shared files are matched by relpath, so `./x` or an absolute path would parse twice.
    for root in roots_for(mod_root, vanilla_path, submod_root):
        path = contained(root, file)
        if path is not None and path.is_file():
            return path.relative_to(root.resolve()).as_posix()
    return file


def assemble_tree_instances(files: list[dict]) -> list[dict]:
    """Assemble every focus_tree in `files` with the shared focuses it imports.

    `files` are `layout_file` results. A shared focus joins a tree when the tree
    imports it by `shared_focus`, or when every shared prerequisite it has is
    already in the tree. Each instance carries `key` (file, tree id, tree line),
    `tree`, `focuses`, and `missing_shared`.
    """
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
    return instances


def layout_file(filepath: str, root: Node, source: str) -> dict:
    """Layout records for one parsed focus file: its trees and its shared focuses."""
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
    return {
        "id": focus_id,
        "file": filepath,
        "line": node_line(node, starts),
        "x": coordinate(node.get("x")),
        "y": coordinate(node.get("y")),
        "relative": symbol_or_str(node.get("relative_position_id")),
        "allow_branch": node.get("allow_branch") is not None,
        "offset": node.get("offset") is not None,
        "prerequisites": focus_prerequisites(node),
        "mutually_exclusive": focus_mutually_exclusive(node),
    }


def coordinate(node: Node | None) -> Decimal | None:
    """A finite numeric `x`/`y`/offset value, or None."""
    # Decimal like upstream, so 0.1 + 0.2 lands on 0.3. Output must still fit a float.
    value = None if node is None else node.value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = repr(value)
    elif isinstance(value, SymbolNode):
        text = value.name
    elif isinstance(value, str):
        text = value
    else:
        return None
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    return result if result.is_finite() and math.isfinite(float(result)) else None


def resolve_positions(
    index: dict[str, list[dict]],
) -> tuple[dict[str, tuple[Decimal, Decimal, bool] | None], dict]:
    """Iterative port of the upstream geometry resolver; handles arbitrarily long chains."""
    positions: dict[str, tuple[Decimal, Decimal, bool] | None] = {}
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
            if not math.isfinite(float(x)) or not math.isfinite(float(y)):
                positions[name] = None
                failures[("coordinates", name)] = (focus, "nonfinite_xy")
            else:
                positions[name] = (x, y, anchor[2] or focus["offset"])
    return positions, failures
