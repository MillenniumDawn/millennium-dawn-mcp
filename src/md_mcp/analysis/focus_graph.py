"""Focus graph queries — prereq/mutex DAG, cycle detection, reachability.

`focus_graph(tag, ...)` returns the dependency graph for a tag's focus tree.

**Output-size aware.** A major country like ISR or USA has 400+ focuses; the
full-detail response would blow past every MCP client output cap. So the tool
has three detail tiers:

  * `detail="summary"` (default)
      Counts + roots + cycles + dangling_prereqs only. Fits in <5 KB.
      Use this first to see the shape of the tree.

  * `detail="ids"`
      Adds `nodes: [{id, line, kind}]` and `edges: [{from, to, kind}]` — enough
      to render structure without metadata. Typically 20–80 KB for a big tag.

  * `detail="full"`
      Full per-node metadata (x, y, cost, icon, prereqs, mutex). Always paginate
      with `node_limit` (default 100) or `focus_ids=[...]` to pin a subset.

Cycles and dangling prereqs are always computed and returned because they're
small and load-bearing for review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..indexes import FocusIndex
from ..paradox import parse_string
from ..paradox.schema import extract_focus_records
from ..util.encoding import read_text
from ..util.response import enforce_budget

_VALID_DETAIL = ("summary", "ids", "full")


def focus_graph(
    tag: str,
    mod_root: Path,
    focus_index: FocusIndex,
    *,
    vanilla_path: Optional[Path] = None,
    detail: str = "summary",
    focus_ids: Optional[Sequence[str]] = None,
    node_limit: int = 100,
    edge_limit: int = 500,
    include_edges: bool = True,
    include_nodes: bool = True,
) -> dict:
    if detail not in _VALID_DETAIL:
        return {
            "ok": False,
            "error": f"Invalid detail '{detail}'. Use one of: {list(_VALID_DETAIL)}",
        }

    focus_index.ensure_fresh()
    tag_upper = tag.upper()
    prefix = tag_upper + "_"
    wanted_ids = {f.upper() for f in focus_ids} if focus_ids else None

    candidate_files = sorted(
        {
            rec["file"]
            for fid in focus_index.list_keys()
            if fid.upper().startswith(prefix) and (rec := focus_index.resolve(fid)) is not None
        }
    )

    # Always parse fully internally — we need the prereq/mutex links for the
    # graph algorithms regardless of which detail tier the caller asked for.
    full_nodes: List[dict] = []
    by_id: Dict[str, dict] = {}

    for relpath in candidate_files:
        abs_path = _resolve(relpath, mod_root, vanilla_path)
        if abs_path is None:
            continue
        try:
            text = read_text(abs_path)
            root = parse_string(text)
        except Exception:
            continue

        for rec in extract_focus_records(root, source=text):
            if not rec["id"].upper().startswith(prefix):
                continue
            entry = {
                "id": rec["id"],
                "file": relpath,
                "line": rec["line"],
                "kind": rec["kind"],
                "x": rec["x"],
                "y": rec["y"],
                "cost": rec["cost"],
                "icon": rec["icon"],
                "prerequisites": rec["prerequisites"],
                "mutually_exclusive": rec["mutually_exclusive"],
                "relative_position_id": rec["relative_position_id"],
            }
            full_nodes.append(entry)
            by_id[rec["id"]] = entry

    # Edges + dangling — always computed; cheap relative to the parse.
    all_edges: List[dict] = []
    dangling: List[dict] = []
    for n in full_nodes:
        for group in n["prerequisites"]:
            for prereq in group:
                all_edges.append({"from": prereq, "to": n["id"], "kind": "prereq"})
                if prereq not in by_id:
                    dangling.append({"focus": n["id"], "missing": prereq})
        for m in n["mutually_exclusive"]:
            all_edges.append({"from": m, "to": n["id"], "kind": "mutex"})

    roots = sorted([n["id"] for n in full_nodes if not n["prerequisites"]])

    prereq_succ: Dict[str, List[str]] = {n["id"]: [] for n in full_nodes}
    for e in all_edges:
        if e["kind"] == "prereq" and e["from"] in prereq_succ:
            prereq_succ[e["from"]].append(e["to"])
    cycles = _find_cycles(prereq_succ)

    # Subset to focus_ids if requested. Done before truncation so the caller's
    # pin always survives.
    if wanted_ids is not None:
        visible_nodes = [n for n in full_nodes if n["id"].upper() in wanted_ids]
        visible_ids = {n["id"] for n in visible_nodes}
        visible_edges = [e for e in all_edges if e["from"] in visible_ids or e["to"] in visible_ids]
    else:
        visible_nodes = full_nodes
        visible_edges = all_edges

    result: dict = {
        "ok": True,
        "tag": tag_upper,
        "detail": detail,
        "node_count": len(full_nodes),
        "edge_count": len(all_edges),
        "roots": roots,
        "cycles": cycles,
        "dangling_prereqs": dangling,
    }

    if detail == "summary":
        # Sample a few node IDs so the agent has something concrete to query next.
        result["sample_node_ids"] = [n["id"] for n in visible_nodes[:20]]
        result["hint"] = (
            "Default detail is 'summary'. Pass detail='ids' for the full id/edge "
            "graph, or detail='full' with focus_ids=[...] for metadata on a subset."
        )
        return result

    # Pagination for ids/full tiers.
    nodes_total = len(visible_nodes)
    edges_total = len(visible_edges)
    nodes_slice = visible_nodes[:node_limit] if include_nodes else []
    edges_slice = visible_edges[:edge_limit] if include_edges else []

    if detail == "ids":
        result["nodes"] = [
            {"id": n["id"], "line": n["line"], "kind": n["kind"], "file": n["file"]}
            for n in nodes_slice
        ]
    else:  # full
        result["nodes"] = nodes_slice

    result["edges"] = edges_slice
    result["nodes_returned"] = len(nodes_slice)
    result["edges_returned"] = len(edges_slice)
    result["nodes_truncated"] = include_nodes and nodes_total > node_limit
    result["edges_truncated"] = include_edges and edges_total > edge_limit

    # Heavy-key drop order: full nodes (largest) before edges, then both.
    return enforce_budget(result, heavy_keys=("nodes", "edges", "cycles"))


def _find_cycles(graph: Dict[str, List[str]]) -> List[List[str]]:
    """Tarjan-style cycle enumeration over a small DAG. Returns one cycle per SCC."""
    cycles: List[List[str]] = []
    color: Dict[str, int] = {n: 0 for n in graph}
    stack: List[str] = []

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt) == 1:
                if nxt in stack:
                    cyc = stack[stack.index(nxt) :] + [nxt]
                    cycles.append(cyc)
            elif color.get(nxt) == 0:
                dfs(nxt)
        color[node] = 2
        stack.pop()

    for n in list(graph.keys()):
        if color[n] == 0:
            dfs(n)
    return cycles


def _resolve(relpath: str, mod_root: Path, vanilla_path: Optional[Path]) -> Optional[Path]:
    p = mod_root / relpath
    if p.exists():
        return p
    if vanilla_path is not None:
        p = vanilla_path / relpath
        if p.exists():
            return p
    return None
