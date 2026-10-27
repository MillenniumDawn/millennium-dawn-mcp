"""Marker-gated real-checkout coverage for the tick_audit and estimate_gdp wrappers.

These tests pin the wrapper/shim adapter against the actual upstream
``tools/analysis/tick_audit.py`` and ``tools/analysis/estimate_gdp.py`` CLI
field names for one country tag, instead of only asserting ``ok``. They skip
without ``MD_MOD_ROOT``, so the unit suite stays mod-free.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from typing import Any, cast

import pytest

import md_mcp.tools.upstream_analysis as upstream_analysis
from tests.support.stdio import server_parameters

_TAG = "GER"


@pytest.mark.integration
def test_tick_audit_matches_upstream_cli_report_within_timeout(real_mod_root, tmp_path):
    report_path = tmp_path / "tick_report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(real_mod_root / "tools" / "analysis" / "tick_audit.py"),
            "--tag",
            _TAG,
            "--json",
            str(report_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=upstream_analysis._TICK_TIMEOUT,
    )
    assert f"Wrote {report_path}" in completed.stdout
    cli = json.loads(report_path.read_text(encoding="utf-8"))

    started = time.monotonic()
    wrapped = upstream_analysis.tick_audit_tool(real_mod_root, tag=_TAG, limit=5, offset=2)
    elapsed = time.monotonic() - started

    assert wrapped["ok"] is True
    assert elapsed < upstream_analysis._TICK_TIMEOUT
    assert wrapped["tag"] == _TAG

    assert wrapped["totals"] == cli["totals"]
    assert wrapped["cadences"].keys() == cli["cadences"].keys()
    for cadence, cli_cadence in cli["cadences"].items():
        assert wrapped["cadences"][cadence] == {
            "global_hooks": cli_cadence["global_hooks"],
            "per_country_hooks": cli_cadence["per_country_hooks"],
            "countries_with_own_hook": len(cli_cadence["countries_with_own_hook"]),
            "global_work_units": cli_cadence["global_work_units"],
            "events_direct": len(cli_cadence["events_direct"]),
            "events_random_pool": len(cli_cadence["events_random_pool"]),
            "timed_decisions": len(cli_cadence["timed_decisions"]),
            "event_loops": len(cli_cadence["event_loops"]),
        }
    assert wrapped["timed_decisions"] == len(cli["timed_decisions"])
    assert wrapped["event_loops"] == len(cli["event_loops"])
    assert wrapped["event_fires"] == len(cli["event_fires"])

    focus_hooks = cli["focus_hooks"]
    assert wrapped["total"] == len(focus_hooks)
    assert wrapped["returned"] == min(5, max(0, len(focus_hooks) - 2))
    assert wrapped["truncated"] is (len(focus_hooks) > 2 + 5)
    for index, hook in enumerate(wrapped["hooks"]):
        expected = focus_hooks[2 + index]
        assert hook == {
            "cadence": expected["cadence"],
            "scope": expected["scope"],
            "file": expected["file"],
            "line": expected["line"],
            "work_units": expected["work_units"],
            "direct_effect_calls": len(expected["direct_effect_calls"]),
            "reached_effects": len(expected["reached_effects"]),
            "events_direct": len(expected["events_direct"]),
            "events_random": len(expected["events_random"]),
        }


@pytest.mark.integration
def test_tick_audit_list_rows_and_tree_match_upstream_report(real_mod_root, tmp_path):
    report_path = tmp_path / "tick_report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(real_mod_root / "tools" / "analysis" / "tick_audit.py"),
            "--json",
            str(report_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=upstream_analysis._TICK_TIMEOUT,
    )
    assert f"Wrote {report_path}" in completed.stdout
    cli = json.loads(report_path.read_text(encoding="utf-8"))

    hooks = upstream_analysis.tick_audit_tool(real_mod_root, list="hooks", limit=5)
    assert hooks["ok"] is True
    assert hooks.get("size_truncated") is not True
    assert hooks["list"] == "hooks" and hooks["total"] == len(cli["hooks"])
    assert hooks["returned"] == min(5, len(cli["hooks"]))
    for row in hooks["rows"]:
        match = [
            h
            for h in cli["hooks"]
            if (h["file"], str(h["line"])) == (row["file"], str(row["line"]))
            and h["cadence"] == row["cadence"]
            and h["scope"] == row["scope"]
        ]
        assert match, row

    events = upstream_analysis.tick_audit_tool(real_mod_root, list="events", limit=5)
    assert events["ok"] is True
    assert events.get("size_truncated") is not True
    assert events["total"] == len(cli["event_fires"])
    assert events["returned"] == min(5, len(cli["event_fires"]))
    for row in events["rows"]:
        record = cli["event_fires"][row["id"]]
        assert row["def_location"] == record["def"]
        assert row["cadences"] == sorted({f["cadence"] for f in record["fired_by"]})
        assert row["scopes"] == sorted({f["scope"] for f in record["fired_by"]})

    decisions = upstream_analysis.tick_audit_tool(real_mod_root, list="decisions", limit=5)
    assert decisions["ok"] is True
    assert decisions.get("size_truncated") is not True
    assert decisions["total"] == len(cli["timed_decisions"])
    assert decisions["returned"] == min(5, len(cli["timed_decisions"]))
    expected_rows = [
        {
            "name": decision["name"],
            "timer_field": decision["timer_field"],
            "timer_value": decision["timer_value"],
            "bucket": decision["bucket"],
            "recurring": decision["recurring"],
            "file": decision["file"],
            "line": decision["line"],
        }
        for decision in cli["timed_decisions"]
    ]
    for row in decisions["rows"]:
        assert row in expected_rows

    tree = upstream_analysis.tick_audit_tool(real_mod_root, tree=True, node_limit=50)
    assert tree["ok"] is True
    assert tree["tree"]["kind"] == "root"
    assert tree["node_limit"] == 50
    assert tree["node_count"] > 0
    assert isinstance(tree["tree_truncated"], bool)

    rejected = upstream_analysis.tick_audit_tool(real_mod_root, list="hooks", cadence="hourly")
    assert rejected["ok"] is False and "cadence" in rejected["error"]


@pytest.mark.integration
def test_estimate_gdp_matches_upstream_cli_fields(real_mod_root):
    completed = subprocess.run(
        [sys.executable, str(real_mod_root / "tools" / "analysis" / "estimate_gdp.py"), _TAG],
        check=True,
        capture_output=True,
        text=True,
        timeout=upstream_analysis._GDP_TIMEOUT,
    )
    out = completed.stdout
    assert re.search(
        rf"^\s+{_TAG} - GDP Estimation \(with modifier stack\)$", out, re.MULTILINE
    ), out

    def cli_field(pattern: str) -> float:
        match = re.search(pattern, out, re.MULTILINE)
        assert match is not None, pattern
        return float(match.group(1))

    cli_states = int(cli_field(r"^  States:\s+(\d+)$"))
    cli_population_m = cli_field(r"^  Population:\s+([\d.]+)M$")
    cli_productivity = cli_field(r"^  Overall Productivity:([\d.]+)$")
    cli_gdp_total = cli_field(r"^  GDP Total:\s+([\d.]+)$")
    cli_gdp_per_capita = cli_field(r"^  GDP per Capita:\s+([\d.]+)$")
    cli_buildings = cli_field(r"^    Buildings:\s+([\d.]+) \(")
    cli_healthcare = cli_field(r"^    Healthcare:\s+([\d.]+) \(")
    cli_agriculture = cli_field(r"^    Agriculture:\s+([\d.]+) \(")
    cli_resources = cli_field(r"^    Resources:\s+([\d.]+) \(")

    wrapped = upstream_analysis.estimate_gdp_tool(real_mod_root, _TAG)

    assert wrapped["ok"] is True
    assert wrapped["tag"] == _TAG
    assert sorted(wrapped) == [
        "breakdown",
        "gdp_per_capita",
        "gdp_total",
        "ok",
        "overall_productivity",
        "population",
        "population_m",
        "states",
        "tag",
    ]
    assert wrapped["states"] == cli_states
    assert wrapped["population_m"] == cli_population_m
    assert round(wrapped["overall_productivity"], 1) == cli_productivity
    assert wrapped["gdp_total"] == cli_gdp_total
    assert wrapped["gdp_per_capita"] == cli_gdp_per_capita
    assert abs(wrapped["population"] - cli_population_m * 1_000_000) <= 5_000
    assert wrapped["breakdown"] == {
        "buildings": cli_buildings,
        "healthcare": cli_healthcare,
        "agriculture": cli_agriculture,
        "resources": cli_resources,
    }
    assert wrapped["gdp_total"] > 0


@pytest.mark.integration
def test_tick_and_gdp_over_real_stdio(real_mod_root, tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    server_params = server_parameters(real_mod_root, tmp_path / "cache")

    async def go():
        async with (
            stdio_client(server_params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tick = await session.call_tool("tick_audit", {"tag": _TAG, "limit": 3, "offset": 1})
            gdp = await session.call_tool("estimate_gdp", {"tag": _TAG})
            return tick, gdp

    tick, gdp = asyncio.run(go())
    tick_payload = json.loads(cast(Any, tick).content[0].text)
    gdp_payload = json.loads(cast(Any, gdp).content[0].text)

    assert tick_payload["ok"] is True
    assert tick_payload["tag"] == _TAG
    assert tick_payload["returned"] == min(3, max(0, tick_payload["total"] - 1))
    assert tick_payload["truncated"] is (tick_payload["total"] > 1 + 3)
    assert tick_payload["hooks"], tick_payload
    assert set(tick_payload["hooks"][0]) == {
        "cadence",
        "scope",
        "file",
        "line",
        "work_units",
        "direct_effect_calls",
        "reached_effects",
        "events_direct",
        "events_random",
    }
    assert "cadences" in tick_payload and "totals" in tick_payload

    assert gdp_payload["ok"] is True
    assert gdp_payload["tag"] == _TAG
    for key in (
        "gdp_total",
        "gdp_per_capita",
        "population",
        "population_m",
        "states",
        "overall_productivity",
        "breakdown",
    ):
        assert key in gdp_payload, key
    assert gdp_payload["breakdown"]
