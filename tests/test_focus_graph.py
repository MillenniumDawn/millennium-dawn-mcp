"""focus_graph analysis tests."""

from __future__ import annotations

from pathlib import Path

from md_mcp.analysis.focus_graph import focus_graph
from md_mcp.indexes import FocusIndex


def test_focus_graph_imports_from_worktree_source():
    expected = Path(__file__).resolve().parents[1] / "src/md_mcp/analysis/focus_graph.py"
    assert Path(focus_graph.__code__.co_filename).resolve() == expected


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


def _write_tree(fake_mod_root, name: str, body: str):
    f = fake_mod_root / "common" / "national_focus" / f"{name}.txt"
    f.write_text(body, encoding="utf-8")


def _paths_for(fake_mod_root, cache_dir, focus_ids, tag="TST"):
    """Run the paths tier once and return its per-focus entries keyed by focus id."""
    fi = FocusIndex(fake_mod_root, cache_dir)
    g = focus_graph(tag, fake_mod_root, fi, detail="paths", focus_ids=focus_ids)
    assert g["ok"]
    return {p["focus"]: p for p in g["paths"]}


def _tree(name: str, focuses: str) -> str:
    return f"focus_tree = {{\n    id = {name}\n{focuses}}}"


def test_paths_no_longer_promises_cheapest_overlap_repro(fake_mod_root, cache_dir):
    """Issue #163 repro 1: OR-group + forced-member overlap must resolve to B+T, not A+B+T.

    T's groups are [A, B] and [B]. The greedy standalone-closure solve used to
    union A (cheapest member of group 1) with B (forced by group 2), giving
    cost 17; B alone satisfies both groups for cost 11.
    """
    body = _tree(
        "overlap_tree",
        """    focus = { id = TST_o_a x = 0 y = 0 cost = 6 }
    focus = { id = TST_o_b x = 1 y = 0 cost = 10 }
    focus = {
        id = TST_o_t x = 0 y = 1 cost = 1
        prerequisite = { focus = TST_o_a focus = TST_o_b }
        prerequisite = { focus = TST_o_b }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_overlap", body)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_o_t"])["TST_o_t"]
    assert p["found"] is True
    assert p["estimate_approximate"] is True
    assert p["estimated_focus_count"] == 2
    assert p["estimated_days"] == 77.0  # (B 10 + T 1) * 7, not (A 6 + B 10 + T 1) * 7
    assert p["chain"] == ["TST_o_b", "TST_o_t"]
    assert "estimate_unreliable" not in p


def test_paths_counts_shared_ancestors_once(fake_mod_root, cache_dir):
    """Or-group members that overlap a pending group cover the shared ancestor once."""
    body = _tree(
        "shared_anc_tree",
        """    focus = { id = TST_s_a x = 0 y = 0 cost = 10 }
    focus = { id = TST_s_b x = 1 y = 0 cost = 10 }
    focus = { id = TST_s_x x = 2 y = 0 cost = 10 }
    focus = {
        id = TST_s_t x = 1 y = 1 cost = 1
        prerequisite = { focus = TST_s_a focus = TST_s_x }
        prerequisite = { focus = TST_s_b focus = TST_s_x }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_shared_anc", body)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_s_t"])["TST_s_t"]
    assert p["estimated_focus_count"] == 2  # X covers both groups; A and B not needed
    assert p["estimated_days"] == 77.0  # (X 10 + T 1) * 7
    assert p["chain"] == ["TST_s_x", "TST_s_t"]


def test_paths_prefers_mutex_feasible_alternative(fake_mod_root, cache_dir):
    """When the cheapest OR member clashes with an already-chosen focus, take the
    next-ranked member whose closure stays mutually-exclusive-feasible."""
    body = _tree(
        "mutex_alt_tree",
        """    focus = {
        id = TST_m_a x = 0 y = 0 cost = 2
        mutually_exclusive = { focus = TST_m_d }
    }
    focus = {
        id = TST_m_d x = 0 y = 1 cost = 1
        mutually_exclusive = { focus = TST_m_a }
    }
    focus = { id = TST_m_b x = 1 y = 0 cost = 9 }
    focus = {
        id = TST_m_t x = 0 y = 2 cost = 1
        prerequisite = { focus = TST_m_d }
        prerequisite = { focus = TST_m_a focus = TST_m_b }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_mutex_alt", body)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_m_t"])["TST_m_t"]
    # A is cheapest but clashes with D (already needed by group 1); B is feasible.
    assert p["estimated_focus_count"] == 3
    assert p["estimated_days"] == 77.0  # (D 1 + B 9 + T 1) * 7
    assert p["chain"] == ["TST_m_b", "TST_m_d", "TST_m_t"]
    assert "mutex_conflicts" not in p


def test_paths_reports_unresolvable_mutex_caveat(fake_mod_root, cache_dir):
    """Caveat: when *no* candidate avoids the mutex clash, keep the cheapest and
    report the conflict instead of silently hoping the plan completes."""
    body = _tree(
        "mutex_bad_tree",
        """    focus = {
        id = TST_q_a x = 0 y = 0 cost = 2
        mutually_exclusive = { focus = TST_q_d }
    }
    focus = {
        id = TST_q_d x = 0 y = 1 cost = 1
        mutually_exclusive = { focus = TST_q_a }
    }
    focus = {
        id = TST_q_b x = 1 y = 0 cost = 9
        mutually_exclusive = { focus = TST_q_d }
    }
    focus = {
        id = TST_q_t x = 0 y = 2 cost = 1
        prerequisite = { focus = TST_q_d }
        prerequisite = { focus = TST_q_a focus = TST_q_b }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_mutex_bad", body)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_q_t"])["TST_q_t"]
    # A is cheapest (2 < 9) and both alternatives clash with D; plan keeps A and
    # reports the D / A overlap as an explicit conflict.
    assert p["found"] is True
    assert p["estimate_approximate"] is True
    assert p["mutex_conflicts"] == [{"focus": "TST_q_a", "mutually_exclusive_with": "TST_q_d"}]
    assert "estimate_unreliable" not in p


def test_paths_prune_resolves_mutex_overlap(fake_mod_root, cache_dir):
    """Bounded pruning drops one side of a mutex pair when the plan stays closed."""
    body = _tree(
        "mutex_prune_tree",
        """    focus = {
        id = TST_r_a x = 0 y = 0 cost = 2
        mutually_exclusive = { focus = TST_r_p }
    }
    focus = {
        id = TST_r_p x = 0 y = 1 cost = 1
        mutually_exclusive = { focus = TST_r_a }
    }
    focus = {
        id = TST_r_t x = 0 y = 2 cost = 1
        prerequisite = { focus = TST_r_a focus = TST_r_p }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_mutex_prune", body)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_r_t"])["TST_r_t"]
    # P satisfies the single group and is mutually exclusive with A; prune drops A.
    assert p["estimated_focus_count"] == 2
    assert p["estimated_days"] == 14.0  # (P 1 + T 1) * 7
    assert "mutex_conflicts" not in p


def test_paths_reports_fully_dangling_group(fake_mod_root, cache_dir):
    """A prerequisite group whose every member is dangling is dropped and reported."""
    body = _tree(
        "dangling_all_tree",
        """    focus = { id = TST_g_p x = 1 y = 0 cost = 3 }
    focus = {
        id = TST_g_t x = 0 y = 1 cost = 1
        prerequisite = { focus = TST_g_missing1 focus = TST_g_missing2 }
        prerequisite = { focus = TST_g_p }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_dangling_all", body)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_g_t"])["TST_g_t"]
    assert p["found"] is True
    assert p["estimated_focus_count"] == 2  # T + P; the dangling group adds nothing
    assert p["estimated_days"] == 28.0  # (T 1 + P 3) * 7
    assert p["unresolved_prereq_groups"] == [
        {"focus": "TST_g_t", "group": ["TST_g_missing1", "TST_g_missing2"]}
    ]
    assert p["dangling_prereqs"] == ["TST_g_missing1", "TST_g_missing2"]


def test_paths_reports_partially_dangling_group(fake_mod_root, cache_dir):
    """A group with one resolvable member proceeds on that member; dangling ids are flat."""
    body = _tree(
        "dangling_partial_tree",
        """    focus = { id = TST_h_p x = 1 y = 0 cost = 3 }
    focus = {
        id = TST_h_t x = 0 y = 1 cost = 1
        prerequisite = { focus = TST_h_missing focus = TST_h_p }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_dangling_partial", body)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_h_t"])["TST_h_t"]
    assert p["estimated_focus_count"] == 2
    assert p["estimated_days"] == 28.0
    assert p["dangling_prereqs"] == ["TST_h_missing"]
    assert "unresolved_prereq_groups" not in p


def test_paths_deterministic_tie_break(fake_mod_root, cache_dir):
    """Equal-cost OR members with equal coverage break ties by id as declared
    (group order in the file), and results are stable across repeated calls."""
    body = _tree(
        "tie_tree",
        """    focus = { id = TST_k_b1 x = 1 y = 0 cost = 5 }
    focus = { id = TST_k_a1 x = 0 y = 0 cost = 5 }
    focus = {
        id = TST_k_t x = 0 y = 1 cost = 1
        prerequisite = { focus = TST_k_b1 focus = TST_k_a1 }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_tie", body)
    fwd = _paths_for(fake_mod_root, cache_dir, ["TST_k_t"])["TST_k_t"]
    assert fwd["estimated_days"] == 42.0  # (B1 5 + T 1) * 7 — first member declared wins
    assert fwd["chain"][0] in ("TST_k_a1", "TST_k_b1")
    assert fwd["chain"][1] == "TST_k_t"
    again = _paths_for(fake_mod_root, cache_dir, ["TST_k_t"])["TST_k_t"]
    assert again == fwd


def test_paths_candidate_closure_budget_fallback(fake_mod_root, cache_dir, monkeypatch):
    body = _tree(
        "closure_budget_tree",
        """    focus = { id = TST_cb_b cost = 1 }
    focus = { id = TST_cb_a cost = 1 prerequisite = { focus = TST_cb_b } }
    focus = { id = TST_cb_t cost = 1 prerequisite = { focus = TST_cb_a } }
""",
    )
    _write_tree(fake_mod_root, "TST_closure_budget", body)
    monkeypatch.setattr("md_mcp.analysis.focus_graph._MAX_PATH_GROUPS", 0)

    result = _paths_for(fake_mod_root, cache_dir, ["TST_cb_t"])["TST_cb_t"]

    assert result["estimate_approximate"] is True
    assert "candidate closure budget stopped early" in result["estimate_unreliable"]


def test_paths_expansion_budget_fallback(fake_mod_root, cache_dir, monkeypatch):
    """Hitting the expansion cap stops cleanly with an unreliable flag, not a hang
    or an exception, and the estimate stays consistent with the partial closure."""
    body = _tree(
        "budget_tree",
        """    focus = { id = TST_e_x x = 0 y = 0 cost = 1 prerequisite = { focus = TST_e_y } }
    focus = { id = TST_e_y x = 1 y = 0 cost = 1 prerequisite = { focus = TST_e_x } }
    focus = {
        id = TST_e_t x = 0 y = 1 cost = 1
        prerequisite = { focus = TST_e_x }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_budget", body)
    monkeypatch.setattr("md_mcp.analysis.focus_graph._MAX_EXPANSION_ROUNDS", 0)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_e_t"])["TST_e_t"]
    assert p["found"] is True
    assert "expansion budget stopped early" in p["estimate_unreliable"]
    monkeypatch.undo()
    fine = _paths_for(fake_mod_root, cache_dir, ["TST_e_t"])["TST_e_t"]
    assert "estimate_unreliable" in fine  # without the cap the cycle flag applies


def test_paths_prune_budget_fallback(monkeypatch, fake_mod_root, cache_dir):
    """A final allowed pass that makes progress conservatively flags the estimate."""
    body = _tree(
        "prune_cap_tree",
        """    focus = {
        id = TST_n_m x = 0 y = 0 cost = 1
        prerequisite = { focus = TST_n_y }
    }
    focus = { id = TST_n_y x = 1 y = 0 cost = 1 }
    focus = { id = TST_n_x x = 2 y = 0 cost = 3 }
    focus = {
        id = TST_n_t x = 0 y = 1 cost = 1
        prerequisite = { focus = TST_n_m focus = TST_n_x }
        prerequisite = { focus = TST_n_x }
    }
""",
    )
    _write_tree(fake_mod_root, "TST_prune_cap", body)
    monkeypatch.setattr("md_mcp.analysis.focus_graph._MAX_PRUNE_PASSES", 1)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_n_t"])["TST_n_t"]
    assert p["found"] is True
    assert "pruning budget stopped early" in p["estimate_unreliable"]
    monkeypatch.undo()
    fine = _paths_for(fake_mod_root, cache_dir, ["TST_n_t"])["TST_n_t"]
    assert "estimate_unreliable" not in fine
    assert fine["estimated_focus_count"] == 2  # T + X; M and Y pruned in later passes


def test_paths_cycle_flag_is_distinct_string(fake_mod_root, cache_dir):
    """A cycle in the closure sets estimate_unreliable and stays a plain string."""
    body = _tree(
        "cycle_str_tree",
        """    focus = { id = TST_c_x x = 0 y = 0 cost = 1 prerequisite = { focus = TST_c_y } }
    focus = { id = TST_c_y x = 1 y = 0 cost = 1 prerequisite = { focus = TST_c_x } }
    focus = { id = TST_c_target x = 0 y = 1 cost = 1 prerequisite = { focus = TST_c_x } }
""",
    )
    _write_tree(fake_mod_root, "TST_cycle_str", body)
    p = _paths_for(fake_mod_root, cache_dir, ["TST_c_target"])["TST_c_target"]
    assert isinstance(p["estimate_unreliable"], str)
    assert "prerequisite cycle" in p["estimate_unreliable"]


def test_paths_missing_focus_entry_stays_minimal(fake_mod_root, cache_dir):
    """found=False entries stay minimal; approximate/flags only appear on found ones."""
    p = _paths_for(fake_mod_root, cache_dir, ["TST_nonexistent"])["TST_nonexistent"]
    assert p == {"focus": "TST_nonexistent", "found": False}


QUOTING_TREE = """focus_tree = {{
    id = {tag}_tree
    focus = {{ id = {q}{tag}_root{q} x = 0 y = 0 }}
    focus = {{
        id = {q}{tag}_a{q} x = 0 y = 1
        prerequisite = {{ focus = {q}{tag}_root{q} }}
        mutually_exclusive = {{ focus = {q}{tag}_b{q} }}
    }}
    focus = {{
        id = {q}{tag}_b{q} x = 1 y = 1
        prerequisite = {{ focus = {q}{tag}_root{q} }}
        mutually_exclusive = {{ focus = {q}{tag}_a{q} }}
    }}
}}
"""


def test_focus_graph_quoted_relations_match_unquoted_twin(fake_mod_root, cache_dir):
    """Quoted focus ids in prerequisite/mutex blocks give the same graph as unquoted ones."""
    for tag, q in (("QQQ", '"'), ("UUU", "")):
        body = QUOTING_TREE.format(tag=tag, q=q)
        f = fake_mod_root / "common" / "national_focus" / f"{tag}_quoting.txt"
        f.write_text(body, encoding="utf-8")
    fi = FocusIndex(fake_mod_root, cache_dir)

    def shape(tag: str) -> tuple[list[str], set[tuple[str, str, str]]]:
        g = focus_graph(tag, fake_mod_root, fi, detail="ids")
        strip = f"{tag}_"
        roots = [r.removeprefix(strip) for r in g["roots"]]
        edges = {
            (e["from"].removeprefix(strip), e["to"].removeprefix(strip), e["kind"])
            for e in g["edges"]
        }
        return roots, edges

    quoted, unquoted = shape("QQQ"), shape("UUU")

    assert quoted == unquoted
    assert unquoted == (
        ["root"],
        {
            ("root", "a", "prereq"),
            ("root", "b", "prereq"),
            ("a", "b", "mutex"),
            ("b", "a", "mutex"),
        },
    )
