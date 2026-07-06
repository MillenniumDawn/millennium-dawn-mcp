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
      Full per-node metadata (x, y, cost, icon, prereqs, mutex, ai_will_do).
      Always paginate with `node_limit` (default 100) or `focus_ids=[...]` to
      pin a subset.

  * `detail="paths"` (requires `focus_ids=[...]`)
      Per requested focus: the transitive prerequisite closure resolved to a
      cheapest completion set, its estimated days (7 days per cost point,
      focus-rush, cheapest member per OR group, shared prereqs counted once),
      the chain in completion order, and the focus's `ai_will_do` summary.

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

_VALID_DETAIL = ("summary", "ids", "full", "paths")

_DEFAULT_FOCUS_COST = 10.0  # vanilla default when a focus omits `cost`
_DAYS_PER_COST = 7.0


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
                "ai_will_do": rec["ai_will_do"],
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

    if detail == "paths":
        if not focus_ids:
            return {"ok": False, "error": "detail='paths' requires focus_ids=[...]"}
        result["paths"] = [_path_entry(req, by_id) for req in focus_ids]
        result["note"] = (
            "estimated_days = 7 days per cost point, uninterrupted focus rush, "
            "cheapest member per OR prerequisite group, shared prereqs counted once. "
            f"Focuses without an explicit cost count as {_DEFAULT_FOCUS_COST:g}."
        )
        return enforce_budget(result, heavy_keys=("paths", "cycles"))

    if detail == "summary":
        # Sample a few node IDs so the agent has something concrete to query next.
        result["sample_node_ids"] = [n["id"] for n in visible_nodes[:20]]
        result["hint"] = (
            "Default detail is 'summary'. Pass detail='ids' for the full id/edge "
            "graph, detail='full' with focus_ids=[...] for metadata on a subset, "
            "or detail='paths' with focus_ids=[...] for prereq chains + timing."
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


def _path_entry(requested: str, by_id: Dict[str, dict]) -> dict:
    """Resolve one focus's cheapest completion set and timing estimate."""
    ci = {fid.upper(): fid for fid in by_id}
    real = ci.get(requested.upper())
    if real is None:
        return {"focus": requested, "found": False}

    memo: Dict[str, tuple] = {}
    dangling: set = set()

    def cost_of(fid: str) -> float:
        c = by_id[fid].get("cost")
        try:
            return float(c) if c is not None else _DEFAULT_FOCUS_COST
        except (TypeError, ValueError):
            return _DEFAULT_FOCUS_COST

    def solve(fid: str, visiting: frozenset) -> Optional[tuple]:
        """Returns (chosen frozenset incl fid, total cost) or None if fid unknown."""
        if fid not in by_id:
            dangling.add(fid)
            return None
        if fid in memo:
            return memo[fid]
        if fid in visiting:
            # Cycle: count self only; cycles are reported at the top level.
            return (frozenset({fid}), cost_of(fid))
        chosen = {fid}
        for group in by_id[fid]["prerequisites"]:
            best: Optional[tuple] = None
            for member in group:
                r = solve(member, visiting | {fid})
                if r is not None and (best is None or r[1] < best[1]):
                    best = r
            if best is not None:
                chosen |= best[0]
        res = (frozenset(chosen), sum(cost_of(f) for f in chosen))
        memo[fid] = res
        return res

    solved = solve(real, frozenset())
    assert solved is not None  # real is in by_id
    chosen, total_cost = solved

    # Completion order: prereq depth within the chosen set, ties by id.
    depth: Dict[str, int] = {}

    def depth_of(fid: str, visiting: frozenset) -> int:
        if fid in depth:
            return depth[fid]
        if fid in visiting:
            return 0
        parents = [
            m for group in by_id[fid]["prerequisites"] for m in group if m in chosen and m != fid
        ]
        d = 1 + max((depth_of(p, visiting | {fid}) for p in parents), default=-1)
        depth[fid] = d
        return d

    chain = sorted(chosen, key=lambda f: (depth_of(f, frozenset()), f))

    defaulted = sum(1 for f in chosen if by_id[f].get("cost") is None)
    entry: dict = {
        "focus": real,
        "found": True,
        "estimated_focus_count": len(chosen),
        "estimated_days": round(total_cost * _DAYS_PER_COST, 1),
        "chain": chain[:100],
        "chain_truncated": len(chain) > 100,
        "ai_will_do": by_id[real].get("ai_will_do"),
    }
    if dangling:
        entry["dangling_prereqs"] = sorted(dangling)
    if defaulted:
        entry["cost_defaulted_count"] = defaulted
    return entry


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
