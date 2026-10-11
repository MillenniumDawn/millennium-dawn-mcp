from __future__ import annotations

import json
import subprocess
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

def _hook(scope, line):
    return {
        "cadence": "daily", "scope": scope, "file": "common/on_actions/test.txt",
        "line": line, "work_units": 3, "direct_effect_calls": ["fx"],
        "reached_effects": ["fx", "fy"], "events_direct": ["evt.1"],
        "events_random": [],
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
        "cadences": cadence, "timed_decisions": [], "event_loops": [],
        "event_fires": {"evt.1": {}}, "hooks": hooks, "focus_hooks": selected,
    }
""",
        encoding="utf-8",
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
    observed = {}

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
        lambda *args, **kwargs: {"ok": True, "hooks": ["x" * 10_000] * 12},
    )

    result = upstream_analysis.tick_audit_tool(tmp_path, limit=20)

    assert result["size_truncated"] is True
    assert result["hooks_dropped"] == 12


def test_subprocess_timeout_is_an_error(monkeypatch, tmp_path):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(upstream_analysis, "run_in_group", timed_out)

    result = upstream_analysis.tick_audit_tool(tmp_path)

    assert result["ok"] is False
    assert result["error"].startswith("tick_audit timed out after 120s:")


def test_subprocess_spawn_failure_is_an_error(monkeypatch, tmp_path):
    def spawn_failed(*args, **kwargs):
        raise OSError("exec failed")

    monkeypatch.setattr(upstream_analysis, "run_in_group", spawn_failed)

    result = upstream_analysis.estimate_gdp_tool(tmp_path, "usa")

    assert result["ok"] is False
    assert "Could not run estimate_gdp" in result["error"]


def test_shim_nonzero_exit_surfaces_stderr_detail(monkeypatch, tmp_path):
    def failing_run(*args, **kwargs):
        return SimpleNamespace(returncode=3, stdout="", stderr="boom upstream\n")

    monkeypatch.setattr(upstream_analysis, "run_in_group", failing_run)

    result = upstream_analysis.estimate_gdp_tool(tmp_path, "usa")

    assert result["ok"] is False
    assert "exited with code 3: boom upstream" in result["error"]


def test_shim_nonzero_exit_without_stderr_falls_back_to_stdout(monkeypatch, tmp_path):
    def failing_run(*args, **kwargs):
        return SimpleNamespace(returncode=2, stdout="stdout trace", stderr="")

    monkeypatch.setattr(upstream_analysis, "run_in_group", failing_run)

    result = upstream_analysis.estimate_gdp_tool(tmp_path, "usa")

    assert result["ok"] is False
    assert "exited with code 2: stdout trace" in result["error"]


def test_shim_invalid_json_is_an_error(monkeypatch, tmp_path):
    def garbage_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(upstream_analysis, "run_in_group", garbage_run)

    result = upstream_analysis.estimate_gdp_tool(tmp_path, "usa")

    assert result["ok"] is False
    assert "returned invalid JSON" in result["error"]


def test_shim_non_object_json_is_an_error(monkeypatch, tmp_path):
    def list_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="[1]", stderr="")

    monkeypatch.setattr(upstream_analysis, "run_in_group", list_run)

    result = upstream_analysis.estimate_gdp_tool(tmp_path, "usa")

    assert result["ok"] is False
    assert "non-object response" in result["error"]


def test_tick_audit_rejects_non_integer_pagination(tmp_path):
    result = upstream_analysis.tick_audit_tool(tmp_path, limit=cast(Any, "lots"))

    assert result["ok"] is False
    assert "limit must be an integer" in result["error"]


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


def test_shim_tick_audit_rejects_bad_pagination(tmp_path):
    shim = _load_shim()
    _write_tick_script(tmp_path)

    result = shim.run("tick_audit", tmp_path, {"limit": "lots"})

    assert result["ok"] is False
    assert "Invalid pagination values" in result["error"]


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


def test_ai_path_report_paginates_section_lists(tmp_path):
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
    result = upstream_analysis.ai_path_report_tool(tmp_path, "USA", "matrix", limit=1, offset=1)
    assert result["ok"] is True
    assert result["matrix"]["rows"] == [2]
    assert result["pagination"]["matrix.rows"] == {"total": 3, "returned": 1, "truncated": True}
    assert "plans" not in result
    nested = upstream_analysis.ai_path_report_tool(
        tmp_path, "USA", "mechanics", limit=10, offset=15
    )
    assert len(nested["mechanics"]["burdens"]) == 20
    assert nested["mechanics"]["burdens"][2]["focus_cures"] == list(range(15, 25))


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
