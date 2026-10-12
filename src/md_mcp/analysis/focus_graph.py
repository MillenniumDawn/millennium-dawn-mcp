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
      Per requested focus: an **approximate** completion set (greedy closure,
      not a proven-minimum plan), estimated days (7 days per cost point,
      focus-rush), the chain in completion order, and the focus's `ai_will_do`
      summary. Per-path entries carry `estimate_approximate` and explicit
      cycle, mutex, and unresolved-prerequisite caveats.
      Entries are capped by `node_limit`.

Cycles and dangling prereqs are always computed and returned because they're
small and load-bearing for review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from ..indexes import FocusIndex
from ..paradox.schema import extract_focus_records
from ..util.response import enforce_budget, paginate
from .scope import iter_scope_files

_VALID_DETAIL = ("summary", "ids", "full", "paths")

_DEFAULT_FOCUS_COST = 10.0  # vanilla default when a focus omits `cost`
_DAYS_PER_COST = 7.0
_MAX_PATH_GROUPS = 4096
_MAX_EXPANSION_ROUNDS = 256
_MAX_PRUNE_PASSES = 8


def focus_graph(
    tag: str,
    mod_root: Path,
    focus_index: FocusIndex,
    *,
    vanilla_path: Optional[Path] = None,
    submod_root: Optional[Path] = None,
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

    tag_upper = tag.upper()
    prefix = tag_upper + "_"
    wanted_ids = {f.upper() for f in focus_ids} if focus_ids else None

    candidate_files = focus_index.files_for_tag(tag)

    # Always parse fully internally — we need the prereq/mutex links for the
    # graph algorithms regardless of which detail tier the caller asked for.
    full_nodes: list[dict] = []
    by_id: dict[str, dict] = {}

    # Files that fail to resolve or parse are left out of the graph.
    for parsed in iter_scope_files(
        candidate_files, mod_root, vanilla_path, [], submod_root=submod_root
    ):
        for rec in extract_focus_records(parsed.root, source=parsed.text):
            if not rec["id"].upper().startswith(prefix):
                continue
            entry = {**rec, "file": parsed.relpath}
            full_nodes.append(entry)
            by_id[rec["id"]] = entry

    # Edges + dangling — always computed; cheap relative to the parse.
    all_edges: list[dict] = []
    dangling: list[dict] = []
    for n in full_nodes:
        for group in n["prerequisites"]:
            for prereq in group:
                all_edges.append({"from": prereq, "to": n["id"], "kind": "prereq"})
                if prereq not in by_id:
                    dangling.append({"focus": n["id"], "missing": prereq})
        for m in n["mutually_exclusive"]:
            all_edges.append({"from": m, "to": n["id"], "kind": "mutex"})

    roots = sorted([n["id"] for n in full_nodes if not n["prerequisites"]])

    prereq_succ: dict[str, list[str]] = {n["id"]: [] for n in full_nodes}
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
        cycle_ids = {fid for cyc in cycles for fid in cyc}
        requested, paths_truncated, paths_total = paginate(focus_ids, 0, node_limit)
        by_upper = {fid.upper(): fid for fid in by_id}
        result["paths"] = [
            _path_entry(req, by_upper.get(req.upper()), by_id, cycle_ids) for req in requested
        ]
        result["paths_total"] = paths_total
        result["paths_returned"] = len(requested)
        result["paths_truncated"] = paths_truncated
        result["note"] = (
            "estimated_days = 7 days per cost point, uninterrupted focus rush, "
            "approximate greedy completion set: cheapest known member per OR "
            "prerequisite group, not a proven-optimal plan (see path caveats). "
            f"Focuses without an explicit cost count as {_DEFAULT_FOCUS_COST:g}."
        )
        return enforce_budget(result, heavy_keys=("paths", "dangling_prereqs", "cycles", "roots"))

    if detail == "summary":
        # Sample a few node IDs so the agent has something concrete to query next.
        result["sample_node_ids"] = [n["id"] for n in visible_nodes[:20]]
        result["hint"] = (
            "Default detail is 'summary'. Pass detail='ids' for the full id/edge "
            "graph, detail='full' with focus_ids=[...] for metadata on a subset, "
            "or detail='paths' with focus_ids=[...] for prereq chains + timing."
        )
        return enforce_budget(result, heavy_keys=("dangling_prereqs", "cycles", "roots"))

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


def _read_cost(c) -> tuple[float, bool]:
    """Return (numeric cost, defaulted). defaulted is True when c is missing or not numeric."""
    try:
        return float(c), False
    except (TypeError, ValueError):
        return _DEFAULT_FOCUS_COST, True


def _path_entry(
    requested: str, real: Optional[str], by_id: dict[str, dict], cycle_ids: set
) -> dict:
    """Resolve one focus's approximate completion set and timing estimate.

    The set is greedy: each candidate closure iteratively takes the cheapest
    known member of each OR group. It does not optimize shared ancestors or
    prove mutex feasibility. The result is an approximation, **not** a
    proven-minimum-cost plan; every found entry sets
    `estimate_approximate: True`. What the heuristic could not resolve is
    reported distinctly rather than folded into the estimate:

    * `estimate_unreliable` (str) — set when the closure contains a
      prerequisite cycle, or a candidate-closure/expansion/prune budget stopped early.
    * `mutex_conflicts` — mutually exclusive focuses that still co-occur in the
      chosen set (an alternative member that avoids the overlap is preferred
      when one exists; unresolved overlaps are caveats, not dropped members).
    * `unresolved_prereq_groups` — groups whose every member is a dangling id,
      dropped from the plan because nothing in them can complete.
    * `dangling_prereqs` — prerequisite ids not found in any focus file.
    """
    if real is None:
        return {"focus": requested, "found": False}

    closure_budget_hit = False

    def cost_of(fid: str) -> float:
        return _read_cost(by_id[fid].get("cost"))[0]

    closure_cache: dict[str, tuple[frozenset, float]] = {}

    def closure_of(fid: str) -> tuple[frozenset, float]:
        """Estimate each candidate closure once per path."""
        nonlocal closure_budget_hit
        if fid in closure_cache:
            return closure_cache[fid]
        closure = {fid}
        seen_groups: set[tuple[str, int]] = set()
        pending = [(fid, i) for i in range(len(by_id[fid]["prerequisites"]))]
        while pending and len(seen_groups) < _MAX_PATH_GROUPS:
            owner, group_index = pending.pop()
            key = (owner, group_index)
            if key in seen_groups:
                continue
            seen_groups.add(key)
            group = by_id[owner]["prerequisites"][group_index]
            if set(group) & closure:
                continue
            candidates = [member for member in group if member in by_id]
            if not candidates:
                continue
            member = min(candidates, key=lambda item: (cost_of(item), item))
            if member not in closure:
                closure.add(member)
                pending.extend((member, i) for i in range(len(by_id[member]["prerequisites"])))
        if pending:
            closure_budget_hit = True
        result = (frozenset(closure), sum(cost_of(item) for item in closure))
        closure_cache[fid] = result
        return result

    def _closure_clashes(cs: frozenset, against: set) -> bool:
        """True if adding cs would put two mutually exclusive focuses in the plan."""
        for a in cs:
            for b in by_id[a]["mutually_exclusive"]:
                if a != b and (b in cs or b in against):
                    return True
        for s in against:
            for b in by_id[s]["mutually_exclusive"]:
                if b in cs:
                    return True
        return False

    chosen: set[str] = {real}
    decided: set[tuple[str, int]] = set()  # (focus id, group index) already handled
    unresolved: list[dict] = []
    reliability: list[str] = []
    rounds = 0
    while True:
        pending: list[tuple[str, int, list[str]]] = []
        for owner in sorted(chosen):
            for gi, group in enumerate(by_id[owner]["prerequisites"]):
                if (owner, gi) in decided or (set(group) & chosen):
                    continue
                pending.append((owner, gi, group))
        if not pending:
            break
        rounds += 1
        if rounds > _MAX_EXPANSION_ROUNDS:
            reliability.append("expansion budget stopped early; closure may be incomplete")
            break
        for owner, gi, group in pending:
            if (owner, gi) in decided or (set(group) & chosen):
                continue  # satisfied by an earlier addition this round
            resolvable = [m for m in group if m in by_id]
            if not resolvable:
                decided.add((owner, gi))
                unresolved.append({"focus": owner, "group": sorted(set(group))})
                continue

            # Rank: cheapest standalone closure, then widest coverage of the
            # round's still-pending groups (shared ancestors), then id, for
            # deterministic ties. Precomputed tuples; the id is unique so the
            # sort never compares the closures themselves.
            scored: list[tuple[float, int, str, frozenset]] = []
            for m in resolvable:
                cs, ccost = closure_of(m)
                covers = sum(1 for _o, _gi, g2 in pending if set(g2) & cs)
                scored.append((ccost, -covers, m, cs))
            scored.sort()

            pick_cs: Optional[frozenset] = None
            for _ccost, _covers, _m, cs in scored:
                if not _closure_clashes(cs, chosen):
                    pick_cs = cs
                    break
            if pick_cs is None:
                # Every candidate clashes with the plan; keep the best-ranked
                # (cheapest) one and let the final mutex scan report it.
                pick_cs = scored[0][3]
            chosen |= set(pick_cs)
            decided.add((owner, gi))

    def _needed_solely(cand: str, pool: set) -> bool:
        """True if some other member of pool is only satisfied by cand right now."""
        for owner in pool:
            if owner == cand:
                continue
            for group in by_id[owner]["prerequisites"]:
                if set(group) & pool == {cand}:
                    return True
        return False

    if closure_budget_hit:
        reliability.append("candidate closure budget stopped early; estimate may be incomplete")

    for pass_no in range(_MAX_PRUNE_PASSES):
        progress = False
        for cand in sorted(chosen - {real}, key=lambda f2: (-cost_of(f2), f2)):
            if cand not in chosen:
                continue
            if not _needed_solely(cand, chosen):
                chosen.discard(cand)
                progress = True
        if not progress:
            break
        if pass_no == _MAX_PRUNE_PASSES - 1:
            reliability.append("pruning budget stopped early; estimate may be larger than needed")

    dangling = {
        m for f2 in chosen for group in by_id[f2]["prerequisites"] for m in group if m not in by_id
    }
    unresolved = [u for u in unresolved if u["focus"] in chosen]
    mutex_conflicts = [
        {"focus": lo, "mutually_exclusive_with": hi}
        for lo, hi in sorted(
            {
                tuple(sorted((a, b)))
                for a in chosen
                for b in by_id[a]["mutually_exclusive"]
                if b in chosen and a != b
            }
        )
    ]

    # Completion order: prereq depth within the chosen set, ties by id.
    depth: dict[str, int] = {}

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

    defaulted = sum(1 for f in chosen if _read_cost(by_id[f].get("cost"))[1])
    entry: dict = {
        "focus": real,
        "found": True,
        # Bounded greedy closure + prune — approximate by construction.
        "estimate_approximate": True,
        "estimated_focus_count": len(chosen),
        "estimated_days": round(sum(cost_of(f) for f in chosen) * _DAYS_PER_COST, 1),
        "chain": chain[:100],
        "chain_truncated": len(chain) > 100,
        "ai_will_do": by_id[real].get("ai_will_do"),
    }
    if chosen & cycle_ids:
        # The closure includes a prereq cycle; the estimate cannot be trusted.
        reliability.insert(0, "prerequisite cycle in closure; see top-level cycles")
    if reliability:
        entry["estimate_unreliable"] = "; ".join(reliability)
    if mutex_conflicts:
        entry["mutex_conflicts"] = mutex_conflicts
    if unresolved:
        entry["unresolved_prereq_groups"] = unresolved
    if dangling:
        entry["dangling_prereqs"] = sorted(dangling)
    if defaulted:
        entry["cost_defaulted_count"] = defaulted
    return entry


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan-style cycle enumeration over a small DAG. Returns one cycle per SCC."""
    cycles: list[list[str]] = []
    color: dict[str, int] = {n: 0 for n in graph}
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt) == 1:
                cycles.append([*stack[stack.index(nxt) :], nxt])
            elif color.get(nxt) == 0:
                dfs(nxt)
        color[node] = 2
        stack.pop()

    for n in list(graph.keys()):
        if color[n] == 0:
            dfs(n)
    return cycles
