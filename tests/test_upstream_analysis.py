from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from src.md_mcp.tools import upstream_analysis


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


def test_calculate_days_matches_fixed_2000_calendar(tmp_path):
    for year, month, day, expected in [
        (2000, 1, 1, 0),
        (2000, 12, 31, 364),
        (2004, 3, 1, 1519),
    ]:
        assert upstream_analysis.calculate_days_tool(tmp_path, year, month, day) == {
            "ok": True,
            "days": expected,
        }


def test_calculate_days_rejects_invalid_dates(tmp_path):
    for year, month, day in [
        (1999, 12, 31),
        (2000, 0, 1),
        (2000, 13, 1),
        (2000, 2, 29),
        (2000, 4, 31),
    ]:
        result = upstream_analysis.calculate_days_tool(tmp_path, year, month, day)
        assert result["ok"] is False
        assert result["error"]


def test_shim_subprocess_uses_devnull_timeout_and_json(monkeypatch, tmp_path):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"ok": true, "days": 0}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = upstream_analysis.calculate_days_tool(tmp_path, 2000, 1, 1)

    assert result == {"ok": True, "days": 0}
    assert observed["command"][1].endswith("upstream_analysis_shim.py")
    assert observed["command"][2:] == [
        "calculate_days",
        str(tmp_path),
        json.dumps({"year": 2000, "month": 1, "day": 1}),
    ]
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["timeout"] == upstream_analysis._CALENDAR_TIMEOUT
    assert observed["capture_output"] is True


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

    monkeypatch.setattr(subprocess, "run", timed_out)

    result = upstream_analysis.tick_audit_tool(tmp_path)

    assert result["ok"] is False
    assert result["error"].startswith("tick_audit timed out after 120s:")
