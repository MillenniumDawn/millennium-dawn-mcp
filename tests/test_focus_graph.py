"""focus_graph analysis tests."""

from __future__ import annotations

from md_mcp.analysis.focus_graph import focus_graph
from md_mcp.indexes import FocusIndex


def test_focus_graph_summary_default(fake_mod_root, cache_dir):
    """Default `detail` is 'summary' — small payload, no nodes/edges arrays."""
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph("TST", fake_mod_root, fi)

    assert g["ok"]
    assert g["tag"] == "TST"
    assert g["detail"] == "summary"
    assert "nodes" not in g
    assert "edges" not in g
    assert g["node_count"] >= 4
    assert g["edge_count"] >= 1
    # Roots/cycles/dangling/sample stay in the summary tier.
    assert "TST_root" in g["roots"]
    assert g["cycles"] == []
    assert g["dangling_prereqs"] == []
    assert g["sample_node_ids"]


def test_focus_graph_ids_tier(fake_mod_root, cache_dir):
    """`detail='ids'` returns id/line/kind per node, no full metadata."""
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph("TST", fake_mod_root, fi, detail="ids")
    assert g["ok"]
    ids = {n["id"] for n in g["nodes"]}
    assert {"TST_root", "TST_branch_a", "TST_branch_b", "TST_shared"} <= ids
    # Node records are trimmed — no `prerequisites` array.
    assert all("prerequisites" not in n for n in g["nodes"])
    # Edges still present.
    edges = {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
    assert ("TST_root", "TST_branch_a", "prereq") in edges
    assert ("TST_root", "TST_branch_b", "prereq") in edges


def test_focus_graph_full_tier(fake_mod_root, cache_dir):
    """`detail='full'` includes every parsed metadata field on each node."""
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph("TST", fake_mod_root, fi, detail="full")
    sample = g["nodes"][0]
    for k in (
        "id",
        "file",
        "line",
        "kind",
        "x",
        "y",
        "cost",
        "prerequisites",
        "mutually_exclusive",
    ):
        assert k in sample, f"missing {k} in full-tier node"
    edges = {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
    assert ("TST_root", "TST_branch_a", "prereq") in edges
    assert ("TST_branch_b", "TST_branch_a", "mutex") in edges or (
        "TST_branch_a",
        "TST_branch_b",
        "mutex",
    ) in edges


def test_focus_graph_focus_ids_subset(fake_mod_root, cache_dir):
    """`focus_ids=[...]` pins the visible nodes to a subset; counts stay accurate."""
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph(
        "TST",
        fake_mod_root,
        fi,
        detail="full",
        focus_ids=["TST_root", "TST_branch_a"],
    )
    visible_ids = {n["id"] for n in g["nodes"]}
    assert visible_ids == {"TST_root", "TST_branch_a"}
    # node_count is still the total across the tag — counts are unchanged by subsetting.
    assert g["node_count"] >= 4


def test_focus_graph_node_limit(fake_mod_root, cache_dir):
    """`node_limit` caps the nodes list and surfaces a truncated flag."""
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph("TST", fake_mod_root, fi, detail="ids", node_limit=2)
    assert len(g["nodes"]) == 2
    assert g["nodes_truncated"] is True


def test_focus_graph_invalid_detail(fake_mod_root, cache_dir):
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph("TST", fake_mod_root, fi, detail="bogus")
    assert g["ok"] is False
    assert "detail" in g["error"]


def test_focus_graph_finds_roots(fake_mod_root, cache_dir):
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph("TST", fake_mod_root, fi)
    assert "TST_root" in g["roots"]
    assert "TST_shared" in g["roots"]


def test_focus_graph_paths_tier(fake_mod_root, cache_dir):
    """detail='paths' returns chain, day estimate, and found=False for unknowns."""
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph(
        "TST", fake_mod_root, fi, detail="paths", focus_ids=["TST_branch_a", "TST_nope"]
    )
    assert g["ok"]
    a = next(p for p in g["paths"] if p["focus"] == "TST_branch_a")
    assert a["found"] is True
    assert a["estimated_focus_count"] == 2
    assert a["estimated_days"] == 105.0  # (10 + 5) * 7
    assert a["chain"] == ["TST_root", "TST_branch_a"]
    missing = next(p for p in g["paths"] if p["focus"] == "TST_nope")
    assert missing["found"] is False


def test_focus_graph_paths_node_limit(fake_mod_root, cache_dir):
    """`node_limit` caps the paths list; totals and the truncated flag stay accurate."""
    fi = FocusIndex(fake_mod_root, cache_dir)
    ids = ["TST_root", "TST_branch_a", "TST_branch_b"]

    g = focus_graph("TST", fake_mod_root, fi, detail="paths", focus_ids=ids, node_limit=2)
    assert g["paths_total"] == 3
    assert g["paths_returned"] == 2
    assert len(g["paths"]) == 2
    assert g["paths_truncated"] is True
    assert [p["focus"] for p in g["paths"]] == ["TST_root", "TST_branch_a"]


def test_focus_graph_paths_untruncated(fake_mod_root, cache_dir):
    """Under the cap, paths reports the full set with truncated=False."""
    fi = FocusIndex(fake_mod_root, cache_dir)
    ids = ["TST_root", "TST_branch_a", "TST_branch_b"]

    g = focus_graph("TST", fake_mod_root, fi, detail="paths", focus_ids=ids)
    assert g["paths_total"] == 3
    assert g["paths_returned"] == 3
    assert len(g["paths"]) == 3
    assert g["paths_truncated"] is False


def test_focus_graph_paths_requires_focus_ids(fake_mod_root, cache_dir):
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph("TST", fake_mod_root, fi, detail="paths")
    assert g["ok"] is False
    assert "focus_ids" in g["error"]


def test_focus_graph_paths_or_group_and_ai_will_do(fake_mod_root, cache_dir):
    """OR prerequisite groups pick the cheapest member; missing cost defaults to 10."""
    body = """focus_tree = {
    id = paths_tree
    focus = {
        id = TST_p_root
        x = 0
        y = 0
        cost = 2
        ai_will_do = { base = 1 modifier = { factor = 0 } }
    }
    focus = {
        id = TST_p_cheap
        x = 0
        y = 1
        cost = 1
        prerequisite = { focus = TST_p_root }
    }
    focus = {
        id = TST_p_pricey
        x = 2
        y = 1
        cost = 8
        prerequisite = { focus = TST_p_root }
    }
    focus = {
        id = TST_leaf
        x = 0
        y = 2
        prerequisite = { focus = TST_p_cheap focus = TST_p_pricey }
        prerequisite = { focus = TST_p_root }
    }
}
"""
    f = fake_mod_root / "common" / "national_focus" / "TST_paths.txt"
    f.write_text(body, encoding="utf-8")
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph("TST", fake_mod_root, fi, detail="paths", focus_ids=["TST_leaf"])
    p = g["paths"][0]
    assert p["found"] is True
    assert p["chain"] == ["TST_p_root", "TST_p_cheap", "TST_leaf"]
    assert p["estimated_days"] == 91.0  # (2 + 1 + 10) * 7
    assert p["cost_defaulted_count"] == 1

    root_entry = focus_graph("TST", fake_mod_root, fi, detail="paths", focus_ids=["TST_p_root"])[
        "paths"
    ][0]
    assert root_entry["ai_will_do"] == {"base": 1, "modifiers": 1}


def test_focus_graph_paths_flags_cycle_unreliable(fake_mod_root, cache_dir):
    """The greedy solve is only sound on a DAG; a prereq cycle in the closure is flagged."""
    body = """focus_tree = {
    id = cyc_tree
    focus = { id = TST_c_x  x = 0 y = 0 cost = 1 prerequisite = { focus = TST_c_y } }
    focus = { id = TST_c_y  x = 1 y = 0 cost = 1 prerequisite = { focus = TST_c_x } }
    focus = { id = TST_c_target x = 0 y = 1 cost = 1 prerequisite = { focus = TST_c_x } }
    focus = { id = TST_c_clean  x = 2 y = 0 cost = 1 }
}
"""
    f = fake_mod_root / "common" / "national_focus" / "TST_cyc.txt"
    f.write_text(body, encoding="utf-8")
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph(
        "TST", fake_mod_root, fi, detail="paths", focus_ids=["TST_c_target", "TST_c_clean"]
    )
    assert g["cycles"]  # the x<->y cycle is detected
    target = next(p for p in g["paths"] if p["focus"] == "TST_c_target")
    clean = next(p for p in g["paths"] if p["focus"] == "TST_c_clean")
    assert "estimate_unreliable" in target
    assert "estimate_unreliable" not in clean
