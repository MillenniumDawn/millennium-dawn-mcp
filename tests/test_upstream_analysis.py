from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import md_mcp.tools.upstream_analysis as upstream_analysis


def _write_tick_script(root: Path) -> None:
    script = root / "tools" / "analysis" / "tick_audit.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        """
print("upstream import noise")

from pathlib import Path

CADENCES = ("daily", "weekly", "monthly")


def _hook(scope, line):
    return {
        "cadence": "daily", "scope": scope, "file": "common/on_actions/test.txt",
        "line": line, "work_units": 3, "direct_effect_calls": ["fx"],
        "reached_effects": ["fx", "fy"], "events_direct": ["evt.1"],
        "events_random": [],
    }


def _decision(name, bucket, line, recurring=True):
    return {
        "name": name, "timer_field": "days", "timer_value": "14", "days": 14,
        "bucket": bucket, "recurring": recurring, "file": "common/decisions/test.txt",
        "line": line,
    }


def index_scripted_effects():
    return {"fx": "x = 1"}


def index_events():
    return {"evt.1": {"file": "events/test_events.txt", "line": 10, "type": "country_event"}}


def index_effect_locations():
    return {"fx": "common/scripted_effects/test.txt:5"}


def build_call_tree(effects, events, loc, max_depth=22, max_nodes=9000):
    assert effects == {"fx": "x = 1"}
    Path(__file__).with_name("tree_built").write_text("built", encoding="utf-8")
    leaf = {"name": "fx", "kind": "effect", "file": loc["fx"], "ops": 1,
            "total": 1, "children": []}
    if max_nodes < 9000:
        leaf["truncated"] = True
    return {
        "name": "Millennium Dawn - recurring ticks", "kind": "root", "ops": 0,
        "total": 2, "children": [
            {"name": "on_daily", "kind": "cadence", "ops": 0, "total": 2,
             "children": [
                 {"name": "on_daily", "kind": "hook",
                  "file": "common/on_actions/test.txt:1", "ops": 2, "total": 2,
                  "children": [leaf]},
             ]},
        ],
    }


def build_report(tag_filter=None):
    hooks = [_hook("GLOBAL", 1), _hook("USA", 2), _hook("CAN", 3)]
    selected = [h for h in hooks if h["scope"] in (tag_filter, "GLOBAL")]
    cadence = {
        "daily": {
            "global_hooks": 1, "per_country_hooks": 2,
            "countries_with_own_hook": ["CAN", "USA"], "global_work_units": 3,
            "events_direct": ["evt.1"], "events_random_pool": [],
            "timed_decisions": [], "event_loops": [],
        }
    }
    return {
        "totals": {"scripted_effects_indexed": 2, "events_indexed": 1},
        "cadences": cadence,
        "timed_decisions": [
            _decision("decision_a", "daily", 20, recurring=False),
            _decision("decision_b", "weekly", 10),
            _decision("decision_c", "monthly", 30),
        ],
        "event_loops": [],
        "event_fires": {
            "evt.1": {
                "def": "events/test_events.txt:10",
                "type": "country_event",
                "fired_by": [
                    {"cadence": "daily", "scope": "GLOBAL",
                     "hook": "common/on_actions/test.txt:1", "kind": "direct"},
                    {"cadence": "daily", "scope": "USA",
                     "hook": "common/on_actions/test.txt:2", "kind": "direct"},
                    {"cadence": "daily", "scope": "CAN",
                     "hook": "common/on_actions/test.txt:3", "kind": "direct"},
                ],
            }
        },
        "hooks": hooks, "focus_hooks": selected,
    }
""",
        encoding="utf-8",
    )


def _enable_spot_scan(root: Path) -> None:
    with (root / "tools" / "analysis" / "tick_audit.py").open("a", encoding="utf-8") as script:
        script.write(
            "\ndef scan_spot_checks(paths=None, scope=None):\n"
            "    return {'mode': 'spot-check', 'filters': {'paths': paths, 'scope': scope},\n"
            "            'summary': {}, 'assumptions': [], 'findings': [], 'diagnostics': []}\n"
        )


def test_tick_audit_isolated_subprocess_filters_and_pages_hooks(tmp_path):
    _write_tick_script(tmp_path)

    result = upstream_analysis.tick_audit_tool(tmp_path, tag="usa", limit=1, offset=1)

    assert result["ok"] is True
    assert result["tag"] == "USA"
    assert result["total"] == 2
    assert result["returned"] == 1
    assert result["truncated"] is False
    assert result["hooks"] == [
        {
            "cadence": "daily",
            "scope": "USA",
            "file": "common/on_actions/test.txt",
            "line": 2,
            "work_units": 3,
            "direct_effect_calls": 1,
            "reached_effects": 2,
            "events_direct": 1,
            "events_random": 0,
        }
    ]
    assert result["cadences"]["daily"]["countries_with_own_hook"] == 2


def _write_gdp_script(root: Path) -> None:
    script = root / "tools" / "analysis" / "estimate_gdp.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        """def parse_all_ideas():
    assert IDEAS_DIR.replace("\\\\", "/").endswith("/common/ideas")
    return {"starting_idea": {"productivity": 1}}

def parse_state_file_from_content(content, name=""):
    owner = content.split("=", 1)[1].strip()
    return {"owner": owner, "name": name}

def compute_country_gdp(tag, states, idea_db):
    assert tag == "USA"
    assert idea_db["starting_idea"]["productivity"] == 1
    assert all(state["owner"] == "USA" for state in states)
    return {
        "gdp_total": 12.345, "gdp_per_capita": 4.567, "population": 200,
        "population_m": 0.2, "num_states": len(states),
        "overall_productivity": 8.765, "gdp_from_buildings_scaled": 1.0,
        "gdp_from_healthcare": 2.0, "gdp_from_agriculture_scaled": 3.0,
        "gdp_from_resources_scaled": 4.0,
    }
""",
        encoding="utf-8",
    )


def test_estimate_gdp_loads_one_tag_and_returns_summary(tmp_path):
    _write_gdp_script(tmp_path)
    states = tmp_path / "history" / "states"
    states.mkdir(parents=True)
    (states / "state_1.txt").write_text("owner = USA\n", encoding="utf-8")
    (states / "state_2.txt").write_text("owner = CAN\n", encoding="utf-8")

    result = upstream_analysis.estimate_gdp_tool(tmp_path, "usa")

    assert result == {
        "ok": True,
        "tag": "USA",
        "gdp_total": 12.35,
        "gdp_per_capita": 4.57,
        "population": 200,
        "population_m": 0.2,
        "states": 1,
        "overall_productivity": 8.77,
        "breakdown": {
            "buildings": 1.0,
            "healthcare": 2.0,
            "agriculture": 3.0,
            "resources": 4.0,
        },
    }


def test_calculate_days_matches_fixed_2000_calendar():
    for year, month, day, expected in [
        (2000, 1, 1, 0),
        (2000, 12, 31, 364),
        (2004, 3, 1, 1519),
    ]:
        assert upstream_analysis.calculate_days_tool(year, month, day) == {
            "ok": True,
            "days": expected,
        }


def test_calculate_days_rejects_invalid_dates():
    for year, month, day in [
        (1999, 12, 31),
        (2000, 0, 1),
        (2000, 13, 1),
        (2000, 2, 29),
        (2000, 4, 31),
    ]:
        result = upstream_analysis.calculate_days_tool(year, month, day)
        assert result["ok"] is False
        assert result["error"]


def test_shim_runs_in_a_process_group_with_timeout_and_json(monkeypatch, tmp_path):
    observed: dict[str, Any] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"ok": true, "tag": "USA"}', stderr="")

    # run_in_group owns stdin=DEVNULL and output capture; tests/test_process.py pins both.
    monkeypatch.setattr(upstream_analysis, "run_in_group", fake_run)

    result = upstream_analysis.estimate_gdp_tool(tmp_path, "usa")

    assert result == {"ok": True, "tag": "USA"}
    assert observed["command"][1].endswith("upstream_analysis_shim.py")
    assert observed["command"][2:] == [
        "estimate_gdp",
        str(tmp_path),
        json.dumps({"tag": "USA"}),
    ]
    assert observed["timeout"] == upstream_analysis._GDP_TIMEOUT
    assert observed["text"] is True


def test_tick_audit_enforces_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda *args, **kwargs: {
            "ok": True,
            "total": 12,
            "returned": 12,
            "truncated": False,
            "hooks": [
                {
                    "cadence": "daily",
                    "scope": "USA",
                    "file": "common/on_actions/" + "界" * 5_000,
                    "line": index,
                    "work_units": 3,
                    "direct_effect_calls": 1,
                    "reached_effects": 2,
                    "events_direct": 1,
                    "events_random": 0,
                }
                for index in range(12)
            ],
        },
    )

    result = upstream_analysis.tick_audit_tool(tmp_path, limit=20)

    assert result["size_truncated"] is True
    assert result["hooks_dropped"] == 12
    assert result["total"] == 12
    assert result["returned"] == 0
    assert result["truncated"] is True
    assert "界" not in json.dumps(result, ensure_ascii=False)


def test_tick_audit_rechecks_budget_after_metadata_correction(monkeypatch, tmp_path):
    from md_mcp.util.response import BUDGET_BYTES

    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda *args, **kwargs: {
            "ok": True,
            "total": 1,
            "diagnostic": "x" * 99_890,
            "hooks": ["界" * 40_000],
        },
    )

    result = upstream_analysis.tick_audit_tool(tmp_path)

    assert result["ok"] is False
    assert result["size_truncated"] is True
    assert "exceeded byte budget" in result["error"]
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_subprocess_timeout_is_an_error(monkeypatch, tmp_path):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(upstream_analysis, "run_in_group", timed_out)

    result = upstream_analysis.tick_audit_tool(tmp_path)

    assert result["ok"] is False
    assert result["error"].startswith("tick_audit timed out after 120s:")


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        pytest.param(OSError("exec failed"), "Could not run estimate_gdp", id="spawn-failure"),
        pytest.param(
            SimpleNamespace(returncode=3, stdout="", stderr="boom upstream\n"),
            "exited with code 3: boom upstream",
            id="nonzero-exit-with-stderr",
        ),
        pytest.param(
            SimpleNamespace(returncode=2, stdout="stdout trace", stderr=""),
            "exited with code 2: stdout trace",
            id="nonzero-exit-falls-back-to-stdout",
        ),
        pytest.param(
            SimpleNamespace(returncode=0, stdout="not json", stderr=""),
            "returned invalid JSON",
            id="invalid-json",
        ),
        pytest.param(
            SimpleNamespace(returncode=0, stdout="[1]", stderr=""),
            "non-object response",
            id="non-object-json",
        ),
    ],
)
def test_shim_failure_is_a_shaped_error(monkeypatch, tmp_path, outcome, error):
    def run(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(upstream_analysis, "run_in_group", run)

    result = upstream_analysis.estimate_gdp_tool(tmp_path, "usa")

    assert result["ok"] is False
    assert error in result["error"]


def test_tick_audit_rejects_unknown_list_and_non_boolean_tree(monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        raise AssertionError("shim must not run for invalid list/tree")

    monkeypatch.setattr(upstream_analysis, "_run_shim", unexpected)

    listed = upstream_analysis.tick_audit_tool(tmp_path, list="loops")
    treed = upstream_analysis.tick_audit_tool(tmp_path, tree=cast(Any, "yes"))

    assert listed["ok"] is False and "list" in listed["error"]
    assert treed["ok"] is False and "tree" in treed["error"]


def test_tick_audit_forwards_list_cadence_tree_and_node_limit(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    def fake_run(mod_root, operation, payload, *, timeout):
        captured.update(operation=operation, payload=payload, timeout=timeout)
        return {"ok": True, "total": 1, "returned": 1, "truncated": False, "hooks": []}

    monkeypatch.setattr(upstream_analysis, "_run_shim", fake_run)

    result = upstream_analysis.tick_audit_tool(
        tmp_path,
        tag="usa",
        list="hooks",
        cadence="weekly",
        tree=True,
        node_limit=50,
        limit=3,
        offset=1,
    )

    assert captured == {
        "operation": "tick_audit",
        "payload": {
            "tag": "USA",
            "limit": 3,
            "offset": 1,
            "list": "hooks",
            "cadence": "weekly",
            "tree": True,
            "node_limit": 50,
        },
        "timeout": upstream_analysis._TICK_TIMEOUT,
    }
    assert result["ok"] is True


def test_tick_audit_budget_drops_tree_before_lists(monkeypatch, tmp_path):
    def oversized(*args, **kwargs):
        return {
            "ok": True,
            "total": 1,
            "returned": 1,
            "truncated": False,
            "hooks": [
                {
                    "cadence": "daily",
                    "scope": "USA",
                    "file": "f",
                    "line": 1,
                    "work_units": 1,
                    "direct_effect_calls": 1,
                    "reached_effects": 1,
                    "events_direct": 1,
                    "events_random": 0,
                }
            ],
            "tree": {"children": "界" * 80_000},
        }

    monkeypatch.setattr(upstream_analysis, "_run_shim", oversized)

    result = upstream_analysis.tick_audit_tool(tmp_path, tree=True)

    assert "tree_dropped" in result
    assert "hooks_dropped" not in result


def test_tick_audit_rows_dropped_resets_pagination_metadata(monkeypatch, tmp_path):
    def oversized_rows(*args, **kwargs):
        return {
            "ok": True,
            "total": 5,
            "returned": 5,
            "truncated": False,
            "list": "hooks",
            "rows": ["界" * 40_000],
        }

    monkeypatch.setattr(upstream_analysis, "_run_shim", oversized_rows)

    result = upstream_analysis.tick_audit_tool(tmp_path, list="hooks")

    assert result["rows_dropped"] == 1
    assert result["returned"] == 0
    assert result["truncated"] is True


def test_tick_spot_check_rejects_bad_pagination_before_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    result = upstream_analysis.tick_spot_check_tool(
        tmp_path, ["events/a.txt"], limit=cast(Any, "lots")
    )
    assert result["ok"] is False
    assert "limit must be an integer" in result["error"]


def test_tick_spot_check_output_budget_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda *args, **kwargs: {
            "ok": True,
            "findings": [{"message": "x" * 30_000} for _ in range(100)],
            "diagnostics": [],
            "assumptions": [],
        },
    )
    result = upstream_analysis.tick_spot_check_tool(tmp_path, ["events/a.txt"])
    assert result.get("size_truncated") or result.get("findings_dropped") is not None
    assert len(str(result)) < 100_000


def test_tick_spot_check_budget_drop_reconciles_pagination(monkeypatch, tmp_path):
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda *args, **kwargs: {
            "ok": True,
            "total": 100,
            "returned": 100,
            "truncated": False,
            "diagnostics_total": 50,
            "diagnostics_returned": 50,
            "diagnostics_truncated": False,
            "findings": [{"message": "x" * 2_000} for _ in range(100)],
            "diagnostics": [{"message": "y" * 2_000} for _ in range(50)],
            "assumptions": [],
        },
    )

    result = upstream_analysis.tick_spot_check_tool(tmp_path, ["events/a.txt"])

    assert result["findings_dropped"] == 100
    assert result["diagnostics_dropped"] == 50
    assert result["returned"] == result["diagnostics_returned"] == 0
    assert result["truncated"] is result["diagnostics_truncated"] is True
    assert len(json.dumps(result).encode("utf-8")) <= 100_000


def test_tick_spot_check_runs_isolated_on_explicit_files_and_pages(tmp_path):
    script = tmp_path / "tools" / "analysis" / "tick_audit.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "def scan_spot_checks(paths=None, scope=None):\n"
        "    return {'mode': 'spot-check', 'filters': {'paths': paths, 'scope': scope},\n"
        "            'summary': {'error': 0}, 'assumptions': [],\n"
        "            'findings': [{'line': n} for n in range(3)],\n"
        "            'diagnostics': [{'line': 4}]}\n",
        encoding="utf-8",
    )
    (tmp_path / "events").mkdir()
    (tmp_path / "events/changed.txt").write_text("x = 1\n", encoding="utf-8")

    result = upstream_analysis.tick_spot_check_tool(
        tmp_path, ["events/changed.txt"], scope="daily", limit=1, offset=1
    )

    assert result["ok"] is True
    assert result["filters"] == {"paths": ["events/changed.txt"], "scope": "daily"}
    assert result["findings"] == [{"line": 1}]
    assert result["total"] == 3 and result["truncated"] is True


def test_tick_spot_check_reads_overlay_file_with_upstream_script_from_base(tmp_path):
    _write_tick_script(tmp_path)
    overlay = tmp_path / "overlay"
    (overlay / "events").mkdir(parents=True)
    (overlay / "events/changed.txt").write_text("overlay = yes\n", encoding="utf-8")
    (overlay / "events/overlay_only.txt").write_text("overlay only = yes\n", encoding="utf-8")
    (tmp_path / "events").mkdir()
    (tmp_path / "events/changed.txt").write_text("base = yes\n", encoding="utf-8")
    (tmp_path / "events/base_only.txt").write_text("base only = yes\n", encoding="utf-8")
    script = tmp_path / "tools" / "analysis" / "tick_audit.py"
    script.write_text(
        "import os\n"
        f"REPO_ROOT = {str(tmp_path)!r}\n"
        "def scan_spot_checks(paths=None, scope=None):\n"
        "    return {'mode': 'spot-check', 'filters': {'paths': paths, 'scope': scope},\n"
        "            'summary': {'contents': {path: open(\n"
        "                os.path.join(REPO_ROOT, path), encoding='utf-8'\n"
        "            ).read().strip() for path in paths}, 'root': REPO_ROOT},\n"
        "            'assumptions': [], 'findings': [],\n"
        "            'diagnostics': []}\n",
        encoding="utf-8",
    )

    result = upstream_analysis.tick_spot_check_tool(
        tmp_path,
        ["events/changed.txt", "events/overlay_only.txt", "events/base_only.txt"],
        submod_root=overlay,
    )

    assert result["ok"] is True
    assert result["filters"]["paths"] == [
        "events/base_only.txt",
        "events/changed.txt",
        "events/overlay_only.txt",
    ]
    assert result["summary"]["contents"] == {
        "events/changed.txt": "overlay = yes",
        "events/overlay_only.txt": "overlay only = yes",
        "events/base_only.txt": "base only = yes",
    }
    assert Path(result["summary"]["root"]).is_relative_to(Path(tempfile.gettempdir()))


@pytest.mark.parametrize("shadow_kind", ["directory", "symlink"])
def test_tick_spot_check_does_not_fall_back_through_unsafe_overlay_shadow(tmp_path, shadow_kind):
    _write_tick_script(tmp_path)
    _enable_spot_scan(tmp_path)
    base_file = tmp_path / "events" / "a.txt"
    base_file.parent.mkdir(parents=True)
    base_file.write_text("base = yes\n", encoding="utf-8")
    overlay = tmp_path / "overlay"
    overlay_file = overlay / "events" / "a.txt"
    overlay_file.parent.mkdir(parents=True)
    if shadow_kind == "directory":
        overlay_file.mkdir()
    else:
        target = overlay / "unsafe-target.txt"
        target.write_text("not a mod file\n", encoding="utf-8")
        overlay_file.symlink_to(target)

    result = upstream_analysis.tick_spot_check_tool(tmp_path, ["events/a.txt"], submod_root=overlay)

    assert result["ok"] is False
    assert result["invalid_files"] == ["events/a.txt"]


@pytest.mark.parametrize("reverse", [False, True])
def test_tick_spot_check_rejects_overlay_base_alias_collision(tmp_path, reverse):
    _write_tick_script(tmp_path)
    _enable_spot_scan(tmp_path)
    base_file = tmp_path / "events" / "a.txt"
    base_file.parent.mkdir(parents=True)
    base_file.write_text("base = yes\n", encoding="utf-8")
    overlay = tmp_path / "overlay"
    overlay_file = overlay / "events" / "a.txt"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text("overlay = yes\n", encoding="utf-8")
    files = ["events/a.txt", str(base_file)]
    if reverse:
        files.reverse()

    result = upstream_analysis.tick_spot_check_tool(tmp_path, files, submod_root=overlay)

    assert result["ok"] is False
    assert set(result["invalid_files"]) == set(files)


def test_tick_spot_check_rejects_empty_files_and_reports_old_upstream(tmp_path):
    empty = upstream_analysis.tick_spot_check_tool(tmp_path, [])
    assert empty["ok"] is False
    assert "at least one changed file" in empty["error"]

    script = tmp_path / "tools" / "analysis" / "tick_audit.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("pass\n", encoding="utf-8")
    result = upstream_analysis.tick_spot_check_tool(tmp_path, ["events/a.txt"])

    assert result["ok"] is False
    assert result["unsupported"] is True
    assert "does not support" in result["error"]


@pytest.mark.integration
def test_tick_spot_check_uses_real_upstream_api_without_widening(real_mod_root):
    candidates = sorted((real_mod_root / "events").rglob("*.txt"))
    if not candidates:
        pytest.skip("No event file available for explicit spot-check integration")
    relpath = candidates[0].relative_to(real_mod_root).as_posix()

    result = upstream_analysis.tick_spot_check_tool(real_mod_root, [relpath], limit=5)

    assert result["ok"] is True
    assert result["filters"]["paths"] == [relpath]
    assert result["returned"] <= 5


def _gdp_returning_none(root: Path) -> None:
    script = root / "tools" / "analysis" / "estimate_gdp.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        """def parse_all_ideas():
    return {}

def parse_state_file_from_content(content, name=""):
    return {"owner": content}

def compute_country_gdp(tag, states, idea_db):
    return None
""",
        encoding="utf-8",
    )


def _load_shim():
    from md_mcp.tools import upstream_analysis_shim

    return upstream_analysis_shim


def test_shim_tick_audit_runs_in_process(tmp_path):
    shim = _load_shim()
    _write_tick_script(tmp_path)

    tagged = shim.run("tick_audit", tmp_path, {"tag": "USA", "limit": 1, "offset": 1})
    assert tagged["ok"] is True
    assert tagged["tag"] == "USA"
    assert tagged["total"] == 2
    assert tagged["returned"] == 1

    untagged = shim.run("tick_audit", tmp_path, {"limit": 2, "offset": 0})
    assert untagged["total"] == 3
    assert untagged["cadences"]["daily"]["countries_with_own_hook"] == 2
    assert "cadence" not in untagged

    weekly = shim.run("tick_audit", tmp_path, {"cadence": "weekly"})
    assert weekly["ok"] is True
    assert weekly["cadence"] == "weekly"
    assert weekly["hooks"] == []
    assert weekly["total"] == 0
    assert weekly["cadences"] == {}


def test_shim_tick_audit_rejects_bad_pagination(tmp_path):
    shim = _load_shim()
    _write_tick_script(tmp_path)

    result = shim.run("tick_audit", tmp_path, {"limit": "lots"})

    assert result["ok"] is False
    assert "Invalid pagination values" in result["error"]


def test_shim_tick_audit_rejects_unknown_list_and_cadence(tmp_path):
    shim = _load_shim()
    _write_tick_script(tmp_path)

    listed = shim.run("tick_audit", tmp_path, {"list": "loops"})
    caded = shim.run("tick_audit", tmp_path, {"list": "hooks", "cadence": "hourly"})

    assert listed["ok"] is False and "list" in listed["error"]
    assert caded["ok"] is False
    assert caded["error"] == "cadence must be one of: daily, weekly, monthly"


def test_shim_tick_audit_list_hooks_pages_rows_in_report_order(tmp_path):
    shim = _load_shim()
    _write_tick_script(tmp_path)

    result = shim.run("tick_audit", tmp_path, {"list": "hooks", "limit": 2, "offset": 1})

    assert result["ok"] is True
    assert result["list"] == "hooks"
    assert result["total"] == 3
    assert result["returned"] == 2
    assert result["truncated"] is False
    # render_list orders hooks by (-work_units, cadence, scope): CAN, GLOBAL, USA.
    assert result["rows"] == [
        {"file": "common/on_actions/test.txt", "line": 1, "cadence": "daily", "scope": "GLOBAL"},
        {"file": "common/on_actions/test.txt", "line": 2, "cadence": "daily", "scope": "USA"},
    ]


def test_shim_tick_audit_list_events_filters_cadence_and_tag(tmp_path):
    shim = _load_shim()
    _write_tick_script(tmp_path)

    untagged = shim.run("tick_audit", tmp_path, {"list": "events"})
    tagged = shim.run("tick_audit", tmp_path, {"list": "events", "tag": "USA"})

    assert untagged["rows"] == [
        {
            "id": "evt.1",
            "cadences": ["daily"],
            "scopes": ["CAN", "GLOBAL", "USA"],
            "def_location": "events/test_events.txt:10",
        }
    ]
    # Mirrors render_list's tag_ok: the focused tag keeps only its own and global hooks.
    assert tagged["rows"] == [
        {
            "id": "evt.1",
            "cadences": ["daily"],
            "scopes": ["GLOBAL", "USA"],
            "def_location": "events/test_events.txt:10",
        }
    ]


def test_shim_tick_audit_list_decisions_filter_cadence(tmp_path):
    shim = _load_shim()
    _write_tick_script(tmp_path)

    all_rows = shim.run("tick_audit", tmp_path, {"list": "decisions"})
    daily = shim.run("tick_audit", tmp_path, {"list": "decisions", "cadence": "daily"})

    # render_list sorts by (bucket, name) alphabetically, so monthly before weekly.
    assert [row["name"] for row in all_rows["rows"]] == [
        "decision_a",
        "decision_c",
        "decision_b",
    ]
    assert set(all_rows["rows"][0]) == {
        "name",
        "timer_field",
        "timer_value",
        "bucket",
        "recurring",
        "file",
        "line",
    }
    assert daily["rows"] == [
        {
            "name": "decision_a",
            "timer_field": "days",
            "timer_value": "14",
            "bucket": "daily",
            "recurring": False,
            "file": "common/decisions/test.txt",
            "line": 20,
        }
    ]


def test_shim_tick_audit_builds_tree_only_when_requested(tmp_path):
    shim = _load_shim()
    _write_tick_script(tmp_path)

    default = shim.run("tick_audit", tmp_path, {})
    assert "tree" not in default
    assert not (tmp_path / "tools" / "analysis" / "tree_built").exists()

    tree = shim.run("tick_audit", tmp_path, {"tree": True})
    listed = shim.run("tick_audit", tmp_path, {"list": "hooks", "tree": True, "node_limit": 2})
    assert tree["tree"]["name"] == "Millennium Dawn - recurring ticks"
    assert listed["rows"] and listed["tree"]["name"] == tree["tree"]["name"]
    assert tree["tree"]["children"][0]["children"][0]["children"][0]["truncated"] is True
    assert tree["tree_truncated"] is True
    assert tree["node_limit"] == 100
    assert tree["node_count"] == 4

    unbounded = shim.run("tick_audit", tmp_path, {"tree": True, "node_limit": 9000})
    assert unbounded["tree_truncated"] is False
    assert unbounded["node_count"] == 4


def test_shim_spot_check_pages_findings_and_diagnostics_directly(tmp_path):
    shim = _load_shim()
    source = tmp_path / "tools/analysis/tick_audit.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def scan_spot_checks(paths=None, scope=None):\n"
        "    return {'findings': [\n"
        "      {'severity': 'high', 'file': paths[0], 'line': n, 'message': 'bad'}\n"
        "      for n in range(3)], 'diagnostics': [\n"
        "      {'fatal': True, 'file': paths[0], 'line': 4},\n"
        "      {'suppressed': True, 'file': paths[0]},\n"
        "      {'file': paths[0]}], 'summary': {'high': 3}}\n",
        encoding="utf-8",
    )
    (tmp_path / "events").mkdir()
    (tmp_path / "events/a.txt").write_text("x = 1\n", encoding="utf-8")

    result = shim.run(
        "tick_spot_check",
        tmp_path,
        {"files": ["events/a.txt"], "limit": 1, "offset": 1},
    )

    assert result["ok"] is True
    assert result["total"] == 3 and result["findings"] == [
        {"severity": "high", "file": "events/a.txt", "line": 1, "message": "bad"}
    ]
    assert result["diagnostics_total"] == 3
    assert result["diagnostics_returned"] == 1
    assert result["diagnostics_summary"] == {
        "total": 3,
        "fatal": 1,
        "suppressed": 1,
        "warning": 1,
    }
    assert result["severity_counts"] == {"error": 4, "warning": 1, "info": 0}


@pytest.mark.parametrize("files", [["events"], ["events/a.gfx"], ["../outside.txt"]])
def test_shim_spot_check_refuses_nonfiles_before_scanner(tmp_path, files):
    shim = _load_shim()
    script = tmp_path / "tools/analysis/tick_audit.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "def scan_spot_checks(paths=None, scope=None):\n"
        "    raise AssertionError('scanner must not run')\n",
        encoding="utf-8",
    )
    (tmp_path / "events").mkdir()
    (tmp_path / "events/a.gfx").write_text("x", encoding="utf-8")
    result = shim.run("tick_spot_check", tmp_path, {"files": files})
    assert result["ok"] is False
    assert result["invalid_files"] == files


def test_shim_spot_check_refuses_symlinks_and_bad_pagination(tmp_path):
    shim = _load_shim()
    script = tmp_path / "tools/analysis/tick_audit.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "def scan_spot_checks(paths=None, scope=None):\n"
        "    raise AssertionError('scanner must not run')\n",
        encoding="utf-8",
    )
    (tmp_path / "events").mkdir()
    target = tmp_path / "events/target.txt"
    target.write_text("x", encoding="utf-8")
    (tmp_path / "events/link.txt").symlink_to(target)
    invalid = shim.run("tick_spot_check", tmp_path, {"files": ["events/link.txt"]})
    assert invalid["ok"] is False
    assert invalid["invalid_files"] == ["events/link.txt"]
    bad_page = shim.run(
        "tick_spot_check", tmp_path, {"files": ["events/target.txt"], "limit": "bad"}
    )
    assert bad_page["ok"] is False
    assert "Invalid pagination" in bad_page["error"]


def test_shim_spot_check_reports_unsupported_and_missing_source(tmp_path):
    shim = _load_shim()
    script = tmp_path / "tools/analysis/tick_audit.py"
    script.parent.mkdir(parents=True)
    script.write_text("pass\n", encoding="utf-8")
    (tmp_path / "events").mkdir()
    (tmp_path / "events/a.txt").write_text("x", encoding="utf-8")
    unsupported = shim.run("tick_spot_check", tmp_path, {"files": ["events/a.txt"]})
    assert unsupported["ok"] is False and unsupported["unsupported"] is True

    script.unlink()
    missing = shim.run("tick_spot_check", tmp_path, {"files": ["events/a.txt"]})
    assert missing["ok"] is False
    assert "Could not load" in missing["error"]


def test_shim_estimate_gdp_rejects_bad_tag(tmp_path):
    shim = _load_shim()

    result = shim.run("estimate_gdp", tmp_path, {"tag": "U$A"})

    assert result["ok"] is False
    assert "tag must be a 2-4 character country tag" in result["error"]


def test_shim_estimate_gdp_reports_missing_states_dir(tmp_path):
    shim = _load_shim()
    _write_gdp_script(tmp_path)

    result = shim.run("estimate_gdp", tmp_path, {"tag": "USA"})

    assert result["ok"] is False
    assert "Could not list state history" in result["error"]


def test_shim_estimate_gdp_reports_no_states_for_tag(tmp_path):
    shim = _load_shim()
    _write_gdp_script(tmp_path)
    states = tmp_path / "history" / "states"
    states.mkdir(parents=True)
    (states / "state_1.txt").write_text("owner = CAN\n", encoding="utf-8")
    (states / "notes.txt.bak").write_text("owner = USA\n", encoding="utf-8")

    result = shim.run("estimate_gdp", tmp_path, {"tag": "USA"})

    assert result["ok"] is False
    assert "No states found for USA" in result["error"]


def test_shim_estimate_gdp_handles_null_gdp_result(tmp_path):
    shim = _load_shim()
    _gdp_returning_none(tmp_path)
    states = tmp_path / "history" / "states"
    states.mkdir(parents=True)
    (states / "state_1.txt").write_text("owner = USA\n", encoding="utf-8")

    result = shim.run("estimate_gdp", tmp_path, {"tag": "USA"})

    assert result["ok"] is False
    assert "returned no result for USA" in result["error"]


def test_shim_estimate_gdp_full_path_in_process(tmp_path):
    shim = _load_shim()
    _write_gdp_script(tmp_path)
    states = tmp_path / "history" / "states"
    states.mkdir(parents=True)
    (states / "state_1.txt").write_text("owner = USA\n", encoding="utf-8")

    result = shim.run("estimate_gdp", tmp_path, {"tag": "USA"})

    assert result["ok"] is True
    assert result["tag"] == "USA"
    assert result["states"] == 1
    assert result["gdp_total"] == 12.35


def test_calculate_days_validates_types_and_ranges():
    assert upstream_analysis.calculate_days_tool(2004, 3, 1) == {"ok": True, "days": 1519}
    bool_year = upstream_analysis.calculate_days_tool(cast(Any, True), 1, 1)
    assert bool_year["ok"] is False
    assert "must be integers" in bool_year["error"]
    assert upstream_analysis.calculate_days_tool(cast(Any, "2000"), 1, 1)["ok"] is False


def test_shim_unknown_operation_is_an_error():
    shim = _load_shim()

    result = shim.run("teleport", Path("/x"), {})

    assert result["ok"] is False
    assert "Unknown operation: teleport" in result["error"]


def test_shim_main_writes_json_and_reports_load_failures(tmp_path, monkeypatch, capsys):
    shim = _load_shim()

    monkeypatch.setattr(
        "sys.argv",
        ["shim", "teleport", str(tmp_path), json.dumps({})],
    )
    assert shim.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out.strip()) == {"ok": False, "error": "Unknown operation: teleport"}

    monkeypatch.setattr(
        "sys.argv",
        ["shim", "tick_audit", str(tmp_path), json.dumps({})],
    )
    shim.main()
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]


def test_shim_load_module_raises_for_directory(tmp_path):
    shim = _load_shim()

    with pytest.raises(ImportError, match="Could not load"):
        shim._load_module(tmp_path, "nope")


def test_event_load_paginates_years_and_returns_flagged_details(tmp_path):
    script = tmp_path / "tools" / "analysis" / "event_load.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        """
def collect(tag, root):
    return {"2000": [("a.1", 1), ("b.1", 2), ("c.1", 70)], "2001": [("d.1", 4)]}
def busiest(days, window):
    return 3 if len(days) > 1 else len(days)
""",
        encoding="utf-8",
    )
    result = upstream_analysis.event_load_tool(tmp_path, limit=1, offset=1, threshold=3)
    assert result["ok"] is True
    assert result["total"] == 2
    assert result["years"][0]["events"][0] == {"id": "b.1", "day_offset": 2}
    assert result["years"][0]["events_truncated"] is True
    assert result["flagged_years"][0]["year"] == "2000"
    assert result["flagged_years"][0]["events"][0] == {"id": "b.1", "day_offset": 2}


@pytest.mark.integration
def test_event_load_matches_upstream_cli(real_mod_root):
    import sys

    script = real_mod_root / "tools" / "analysis" / "event_load.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--tag",
            "USA",
            "--window",
            "45",
            "--threshold",
            "3",
            "--path",
            str(real_mod_root),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    cli = json.loads(completed.stdout)
    wrapped = upstream_analysis.event_load_tool(
        real_mod_root, tag="USA", window=45, threshold=3, limit=1000
    )
    assert wrapped["ok"] is True
    for year, summary in cli["years"].items():
        row = next(item for item in wrapped["years"] if item["year"] == year)
        assert row["total"] == summary["count"]
        assert row["busiest_window"] == summary["peak"]


def test_event_load_defaults_validation_and_budget(tmp_path):
    from md_mcp.tools.upstream_analysis_shim import _event_load, run

    script = tmp_path / "tools" / "analysis" / "event_load.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        """
def collect(tag, root):
    return {"2000": [("e.%d" % i, i) for i in range(1800)]}
def busiest(days, window):
    return len(days)
""",
        encoding="utf-8",
    )
    defaults = run("event_load", tmp_path, {})
    assert defaults["window"] == 45
    assert defaults["threshold"] == 3
    invalid = _event_load(tmp_path, {"tag": "USA.*"})
    assert invalid["ok"] is False
    invalid_num = _event_load(tmp_path, {"window": "bad"})
    assert invalid_num["ok"] is False
    large = upstream_analysis.event_load_tool(tmp_path, limit=1800)
    assert large["size_truncated"] is True
    assert large["total"] == 1
    assert large["returned"] == 0
    assert large["truncated"] is True


def test_event_load_missing_upstream_script_is_reported(tmp_path):
    result = upstream_analysis.event_load_tool(tmp_path)
    assert result["ok"] is False
    assert "error" in result


def test_game_log_summary_requires_absolute_path_and_paginates_lists(tmp_path):
    script = tmp_path / "tools" / "summarize_game_log.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        """
import json
from collections import Counter
def parse(path, since=None, until=None):
    return {"stats": {"parsed": 2}, "activity": Counter({"Brazil": 3}), "decisions": {}}
def pick_countries(data, requested, top_countries):
    chosen = []
    for request in requested:
        match = next(
            (name for name in data["activity"] if request.lower() in name.lower()), request
        )
        if match not in chosen:
            chosen.append(match)
    if not chosen:
        chosen = [data["activity"].most_common(1)[0][0]]
    return chosen
def to_json(data, countries=None, top=15):
    return json.dumps({
        "conflicts": [{"id": "a"}, {"id": "b"}],
        "politics": [],
        "annexations": [],
        "most_active": [["USA", 2]][:top],
        "focus_countries": {country: {"detail": True} for country in (countries or [])},
    })
def parse_date_arg(value):
    return value
""",
        encoding="utf-8",
    )
    log = tmp_path / "game.log"
    log.write_text("readonly", encoding="utf-8")
    bad = upstream_analysis.game_log_summary_tool(tmp_path, "relative.log")
    assert bad["ok"] is False
    result = upstream_analysis.game_log_summary_tool(
        tmp_path,
        str(log),
        limit=1,
        offset=0,
        countries=["USA"],
        since="2001.1.1",
        until="2002.1.1",
    )
    assert result["ok"] is True
    assert result["conflicts"] == [{"id": "a"}]
    assert result["conflicts_total"] == 2
    assert result["focus_countries"] == {"USA": {"detail": True}}
    assert result["focus_countries_total"] == 1
    implicit = upstream_analysis.game_log_summary_tool(tmp_path, str(log))
    partial = upstream_analysis.game_log_summary_tool(tmp_path, str(log), countries=["braz"])
    lower = upstream_analysis.game_log_summary_tool(tmp_path, str(log), countries=["brazil"])
    for resolved in (implicit, partial, lower):
        assert resolved["focus_countries"] == {"Brazil": {"detail": True}}
    next_page = upstream_analysis.game_log_summary_tool(tmp_path, str(log), limit=1, offset=1)
    assert next_page["conflicts"] == [{"id": "b"}]
    assert log.read_text(encoding="utf-8") == "readonly"


def test_game_log_summary_rejects_invalid_numeric_options_before_running_shim(tmp_path):
    result = upstream_analysis.game_log_summary_tool(
        tmp_path, "/tmp/game.log", top=cast(Any, "many")
    )

    assert result["ok"] is False
    assert "top must be an integer" in result["error"]


def _write_summary_shim_script(root: Path, *, parsed: int = 2) -> None:
    script = root / "tools" / "summarize_game_log.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f"""import json
def parse(path, since=None, until=None):
    return {{"stats": {{"parsed": {parsed}}}, "dates": (since, until)}}
def parse_date_arg(value):
    return value
def pick_countries(data, requested, top_countries):
    return requested or ["BRA"]
def to_json(data, countries=None, top=15):
    return json.dumps({{
        "conflicts": [{{"id": "a"}}, {{"id": "b"}}],
        "politics": [{{"id": "p1"}}, {{"id": "p2"}}, {{"id": "p3"}}],
        "annexations": [],
        "economy": {{"BRA": {{"gdp": 1}}, "CAN": {{"gdp": 2}}}},
        "inflation": {{"BRA": {{"rate": 1}}, "CAN": {{"rate": 2}}}},
        "focus_countries": {{
            country: {{"focus": True}} for country in (countries or []) + ["MEX"]
        }},
        "most_active": countries[:top],
    }})
""",
        encoding="utf-8",
    )


def test_shim_game_log_summary_validates_inputs_and_pages_summary_maps(tmp_path):
    shim = _load_shim()
    _write_summary_shim_script(tmp_path)
    log = tmp_path / "game.log"
    log.write_text("readonly", encoding="utf-8")

    result = shim.run(
        "game_log_summary",
        tmp_path,
        {
            "path": str(log),
            "top": 2,
            "limit": 1,
            "offset": 1,
            "countries": ["CAN"],
            "since": "2001.1.1",
            "until": "2002.1.1",
        },
    )

    assert result["ok"] is True
    assert result["path"] == str(log)
    assert result["offset"] == 1
    assert result["limit"] == 1
    assert result["conflicts"] == [{"id": "b"}]
    assert result["conflicts_total"] == 2
    assert result["conflicts_returned"] == 1
    assert result["conflicts_truncated"] is False
    assert result["politics"] == [{"id": "p2"}]
    assert result["politics_total"] == 3
    assert result["politics_truncated"] is True
    assert result["economy"] == {"CAN": {"gdp": 2}}
    assert result["inflation"] == {"CAN": {"rate": 2}}
    assert result["focus_countries"] == {"MEX": {"focus": True}}
    assert result["most_active"] == ["CAN"]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"path": "relative.log"}, "absolute path"),
        ({"path": "/tmp/session.csv"}, "end in .log or .txt"),
        ({"path": "/tmp/missing.log"}, "Log file not found"),
    ],
)
def test_shim_game_log_summary_rejects_invalid_paths(tmp_path, payload, error):
    result = _load_shim().run("game_log_summary", tmp_path, payload)
    assert result["ok"] is False
    assert error in result["error"]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"top": "many"}, "Invalid numeric parameter"),
        ({"countries": ["BRA", 2]}, "countries must be a list"),
    ],
)
def test_shim_game_log_summary_rejects_invalid_summary_options(tmp_path, payload, error):
    _write_summary_shim_script(tmp_path)
    log = tmp_path / "game.txt"
    log.write_text("readonly", encoding="utf-8")
    result = _load_shim().run("game_log_summary", tmp_path, {"path": str(log), **payload})
    assert result["ok"] is False
    assert error in result["error"]


def test_shim_game_log_summary_rejects_logs_without_parsed_entries(tmp_path):
    _write_summary_shim_script(tmp_path, parsed=0)
    log = tmp_path / "empty.log"
    log.write_text("not a scripted entry", encoding="utf-8")

    result = _load_shim().run("game_log_summary", tmp_path, {"path": str(log)})

    assert result == {
        "ok": False,
        "path": str(log),
        "error": "No scripted MD log entries found",
    }


def test_shim_game_log_summary_reports_parser_errors(tmp_path):
    script = tmp_path / "tools" / "summarize_game_log.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "def parse(path, since=None, until=None):\n"
        "    raise ValueError('invalid date range')\n"
        "def parse_date_arg(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    log = tmp_path / "bad.log"
    log.write_text("bad dates", encoding="utf-8")

    result = _load_shim().run("game_log_summary", tmp_path, {"path": str(log)})

    assert result == {
        "ok": False,
        "path": str(log),
        "error": "invalid date range",
    }


def test_game_log_summary_bounds_large_economy_and_inflation_maps(tmp_path):
    script = tmp_path / "tools" / "summarize_game_log.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        """
import json
def parse(path, since=None, until=None):
    return {"stats": {"parsed": 1}}
def parse_date_arg(value):
    return value
def pick_countries(data, requested, top_countries):
    return requested
def to_json(data, countries=None, top=15):
    return json.dumps({"economy": {"C%03d" % i: {} for i in range(700)},
                       "inflation": {"C%03d" % i: {} for i in range(700)},
                       "conflicts": [], "politics": [], "annexations": [],
                       "focus_countries": {}})
""",
        encoding="utf-8",
    )
    log = tmp_path / "large.txt"
    log.write_text("readonly", encoding="utf-8")
    result = upstream_analysis.game_log_summary_tool(tmp_path, str(log), top=1, limit=1)
    assert result["ok"] is True
    assert result["economy_total"] == 700
    assert result["inflation_total"] == 700
    assert len(result["economy"]) == len(result["inflation"]) == 1


@pytest.mark.parametrize(
    "key",
    ("focus_countries", "economy", "inflation", "conflicts", "politics", "annexations"),
)
def test_game_log_summary_budget_drop_updates_collection_metadata(monkeypatch, tmp_path, key):
    log = tmp_path / "game.log"
    log.write_text("readonly", encoding="utf-8")
    data = (
        [{"amount": "界" * 5_000} for _ in range(12)]
        if key in {"conflicts", "politics", "annexations"}
        else {f"C{index:03d}": {"amount": "界" * 5_000} for index in range(12)}
    )
    total_key = f"{key}_total"
    returned_key = f"{key}_returned"
    truncated_key = f"{key}_truncated"
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda *args, **kwargs: {
            "ok": True,
            "path": str(log),
            key: data,
            total_key: 12,
            returned_key: 12,
            truncated_key: False,
        },
    )

    result = upstream_analysis.game_log_summary_tool(tmp_path, str(log))

    assert result["size_truncated"] is True
    assert result[f"{key}_dropped"] == 12
    assert result[total_key] == 12
    assert result[returned_key] == 0
    assert result[truncated_key] is True
    assert "界" not in json.dumps(result, ensure_ascii=False)


def test_game_log_summary_budget_fallback_keeps_error_and_hides_payload(monkeypatch, tmp_path):
    log = tmp_path / "game.log"
    log.write_text("readonly", encoding="utf-8")
    secret = "private-log-content-" * 10_000
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda *args, **kwargs: {
            "ok": True,
            "path": str(log),
            "economy": {"USA": {"amount": "界" * 5_000}},
            "economy_total": 1,
            "economy_returned": 1,
            "economy_truncated": False,
            "diagnostic": secret,
        },
    )

    result = upstream_analysis.game_log_summary_tool(tmp_path, str(log))

    assert result["ok"] is False
    assert result["size_truncated"] is True
    assert "exceeded byte budget" in result["error"]
    assert secret not in json.dumps(result)


def test_game_log_summary_budget_drop_keeps_missing_total_unknown(monkeypatch, tmp_path):
    log = tmp_path / "game.log"
    log.write_text("readonly", encoding="utf-8")
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda *args, **kwargs: {
            "ok": True,
            "path": str(log),
            "economy": {"USA": {"amount": "界" * 40_000}},
            "economy_returned": 1,
            "economy_truncated": False,
        },
    )

    result = upstream_analysis.game_log_summary_tool(tmp_path, str(log))

    assert "economy_total" not in result
    assert result["economy_returned"] == 0
    assert result["economy_truncated"] is True


@pytest.mark.integration
def test_game_log_summary_matches_cli_country_resolution_and_filters(real_mod_root, tmp_path):
    script = real_mod_root / "tools" / "summarize_game_log.py"
    log = tmp_path / "fixture.log"
    log.write_text(
        """
[00:00:00][2002.01.01.01][effectbase.cpp:1]: 1:00, 1 Jan, 2002: Brazil: Decision first
[00:00:01][2003.01.01.01][effectbase.cpp:1]: 1:00, 1 Jan, 2003: Brazil: Decision second
[00:00:02][2004.01.01.01][effectbase.cpp:1]: 1:00, 1 Jan, 2004: Canada: Focus CAN_example
""",
        encoding="utf-8",
    )
    for country in (None, "brazil", "Bra"):
        args = [sys.executable, str(script), str(log), "--json"]
        if country is not None:
            args.extend(["--country", country])
        args.extend(["--since", "2003.1.1", "--until", "2004.12.31"])
        cli = json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)
        wrapped = upstream_analysis.game_log_summary_tool(
            real_mod_root,
            str(log),
            countries=[country] if country else None,
            since="2003.1.1",
            until="2004.12.31",
        )
        for key in (
            "session",
            "categories",
            "most_active",
            "conflicts",
            "politics",
            "annexations",
            "economy",
            "inflation",
            "focus_countries",
        ):
            assert wrapped[key] == cli[key]


def test_ai_path_report_paginates_section_lists(tmp_path):
    from md_mcp.tools.upstream_analysis_shim import _ai_path_report, run

    script = tmp_path / "tools" / "analysis" / "ai_path_report.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        """
SECTIONS = (
    "rule", "wiring", "owners", "matrix", "graph", "plans", "rewards", "mechanics", "government"
)
def build_report(root, tag, limit):
    return {
        "tag": tag, "focus_count": 2,
        "matrix": {"rows": [1, 2, 3]}, "plans": {"items": ["a", "b"]},
        "mechanics": {"burdens": [
            {"focus_cures": list(range(40))} for _ in range(20)
        ]}
    }
""",
        encoding="utf-8",
    )
    result = run(
        "ai_path_report",
        tmp_path,
        {"tag": "USA", "section": "matrix", "limit": 1, "offset": 1},
    )
    assert result["ok"] is True
    assert result["matrix"]["rows"] == [2]
    assert result["pagination"]["matrix.rows"] == {"total": 3, "returned": 1, "truncated": True}
    assert "plans" not in result
    nested = _ai_path_report(
        tmp_path, {"tag": "USA", "section": "mechanics", "limit": 10, "offset": 15}
    )
    assert len(nested["mechanics"]["burdens"]) == 20
    assert nested["mechanics"]["burdens"][2]["focus_cures"] == list(range(15, 25))
    assert _ai_path_report(tmp_path, {"tag": "bad!"})["ok"] is False
    assert _ai_path_report(tmp_path, {"tag": "USA", "limit": "bad"})["ok"] is False
    assert _ai_path_report(tmp_path, {"tag": "USA", "section": 3})["ok"] is False
    assert _ai_path_report(tmp_path, {"tag": "USA", "section": "unknown"})["ok"] is False
    assert _ai_path_report(tmp_path, {"tag": "USA"})["ok"] is True
    assert _ai_path_report(tmp_path, {"tag": "USA", "section": ["matrix"]})["ok"] is True
    assert (
        upstream_analysis.ai_path_report_tool(tmp_path, "USA", limit=cast(Any, "bad"))["ok"]
        is False
    )


def test_ai_path_report_tool_validates_offset_and_budget(monkeypatch, tmp_path):
    observed: dict[str, Any] = {}

    def fake_run(root, operation, payload, *, timeout):
        observed.update(operation=operation, payload=payload, timeout=timeout)
        return {"ok": True, "focus_count": 1}

    monkeypatch.setattr(upstream_analysis, "_run_shim", fake_run)
    result = upstream_analysis.ai_path_report_tool(tmp_path, "USA", limit=4, offset=2)
    assert result["ok"] is True
    assert observed["operation"] == "ai_path_report"
    assert observed["payload"]["offset"] == 2
    invalid = upstream_analysis.ai_path_report_tool(tmp_path, "USA", offset=cast(Any, "bad"))
    assert invalid["ok"] is False


@pytest.mark.integration
def test_ai_path_report_real_checkout(real_mod_root):
    result = upstream_analysis.ai_path_report_tool(real_mod_root, "USA", "matrix", limit=2)
    assert result["ok"] is True
    assert result["tag"] == "USA"
    assert "matrix" in result
    mechanics = upstream_analysis.ai_path_report_tool(
        real_mod_root, "USA", "mechanics", limit=15, offset=15
    )
    assert mechanics["ok"] is True
    burdens = mechanics["mechanics"]["burdens"]
    assert len(burdens) == mechanics["pagination"]["mechanics.burdens"]["total"]
    assert burdens[2]["focus_cures"]
