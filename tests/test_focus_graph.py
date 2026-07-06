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
