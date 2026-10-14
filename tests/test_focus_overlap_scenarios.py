from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import types
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from md_mcp.tools import upstream_analysis, upstream_analysis_shim


@dataclass
class _Scenario:
    tag: str
    completed: frozenset[str]
    date: tuple[int, int, int] | None


@dataclass
class _Offset:
    dx: int = 0
    dy: int = 0
    trigger: str = ""


@dataclass
class _Focus:
    id: str
    line: int
    kind: str = "focus"
    x: int = 0
    y: int = 0
    relative_to: str | None = None
    allow_branch: str | None = None
    offsets: list[_Offset] = field(default_factory=list)
    prereq_groups: list[list[str]] = field(default_factory=list)


def _fake_focuses() -> dict[str, _Focus]:
    return {
        "A": _Focus("A", 1, x=0, offsets=[_Offset(dx=2, trigger="date > 2005.1.1")]),
        "B": _Focus("B", 4, x=2, allow_branch="has_completed_focus = A"),
        "C": _Focus("C", 7, x=5, offsets=[_Offset(dx=9, trigger="mystery = yes")]),
        "MISSING": _Focus("MISSING", 10, relative_to="ABSENT"),
        "CYCLE_A": _Focus("CYCLE_A", 13, relative_to="CYCLE_B"),
        "CYCLE_B": _Focus("CYCLE_B", 16, relative_to="CYCLE_A"),
    }


def _fake_api(monkeypatch, root: Path):
    (root / "tools" / "analysis").mkdir(parents=True, exist_ok=True)
    helper_path = root / "tools" / "analysis" / "focus_overlap_report.py"
    helper_path.write_text("# API is injected by this test\n", encoding="utf-8")
    module = types.SimpleNamespace()
    module.Scenario = _Scenario
    module.parse_date = lambda text: tuple(int(part) for part in text.split("."))

    def iter_focus_blocks(text):
        return [
            (focus_id, "focus", line, "")
            for line, focus_id in enumerate(re.findall(r"\bid\s*=\s*(\w+)", text), 1)
        ]

    def parse_focus_file(text):
        available = _fake_focuses()
        return {
            focus_id: available[focus_id]
            for focus_id, _kind, _line, _body in iter_focus_blocks(text)
            if focus_id in available
        }

    module.iter_focus_blocks = iter_focus_blocks
    module.parse_focus_file = parse_focus_file

    def evaluate(body: str, scenario: _Scenario):
        if body.startswith("date >"):
            if scenario.date is None:
                return None
            return scenario.date > (2005, 1, 1)
        if body.startswith("has_completed_focus ="):
            return body.split("=", 1)[1].strip() in scenario.completed
        return None

    module.evaluate = evaluate

    def resolve_positions(focuses, scenario):
        positions = {}

        def resolve(focus_id, stack=frozenset()):
            focus = focuses[focus_id]
            x, y = focus.x, focus.y
            for offset in focus.offsets:
                if evaluate(offset.trigger, scenario) is True:
                    x += offset.dx
                    y += offset.dy
            parent = focus.relative_to
            if parent in focuses and parent not in stack:
                px, py = resolve(parent, stack | {focus_id})
                return px + x, py + y
            return x, y

        for focus_id in focuses:
            positions[focus_id] = resolve(focus_id)
        return positions

    module.resolve_positions = resolve_positions
    module.resolve_visibility = lambda focuses, scenario: {
        focus_id: focus.allow_branch is None or evaluate(focus.allow_branch, scenario) is not False
        for focus_id, focus in focuses.items()
    }

    def build_report(focuses, positions, visible, scenario, region, adjacent, show_map):
        cells = defaultdict(list)
        for focus_id in focuses:
            if visible[focus_id]:
                cells[positions[focus_id]].append(focus_id)
        collisions = [
            {
                "x": x,
                "y": y,
                "focuses": [
                    {"id": focus_id, "line": focuses[focus_id].line, "x": x, "y": y}
                    for focus_id in ids
                ],
            }
            for (x, y), ids in sorted(cells.items())
            if len(ids) > 1
        ]
        report = {
            "scenario": {
                "tag": scenario.tag,
                "completed": sorted(scenario.completed),
                "date": ".".join(map(str, scenario.date)) if scenario.date else None,
            },
            "focuses": len(focuses),
            "visible": sum(visible.values()),
            "collisions": collisions,
        }
        if show_map:
            report["map"] = [
                {"id": focus_id, "line": focuses[focus_id].line, "x": xy[0], "y": xy[1]}
                for focus_id, xy in positions.items()
                if visible[focus_id]
            ]
        return report

    module.build_report = build_report

    def load_module(path: Path, name: str):
        if name == "shared_utils":
            utility = types.ModuleType("shared_utils")
            utility.strip_comments = lambda text: text
            utility.blank_quoted_strings = lambda text: text
            sys.modules["shared_utils"] = utility
            return utility
        if name == "_md_mcp_upstream_focus_overlap_report":
            utility = types.ModuleType("shared_utils")
            utility.strip_comments = lambda text: text
            utility.blank_quoted_strings = lambda text: text
            sys.modules["shared_utils"] = utility
            return module
        raise AssertionError(name)

    monkeypatch.setattr(upstream_analysis_shim, "_load_module", load_module)
    return module


def test_shim_evaluates_explicit_scenario_and_reports_uncertainty(monkeypatch, tmp_path):
    root = tmp_path / "mod"
    focus_file = root / "common/national_focus/test.txt"
    focus_file.parent.mkdir(parents=True)
    focus_file.write_text(
        "focus_tree = {\n"
        ' focus = { id = A x = 0 y = 0 title = "brace } {" # {\n }\n'
        " focus = { id = A x = 0 y = 0 }\n"
        " focus = { id = B x = 0 y = 0 }\n"
        " focus = { id = C x = 0 y = 0 }\n"
        " focus = { id = MISSING x = 0 y = 0 }\n"
        " focus = { id = CYCLE_A x = 0 y = 0 }\n"
        " focus = { id = CYCLE_B x = 0 y = 0 }\n"
        "}\n",
        encoding="utf-8",
    )
    _fake_api(monkeypatch, root)

    result = upstream_analysis_shim._focus_overlap_scenarios(
        root,
        {
            "file": str(focus_file),
            "allowed_roots": [str(root)],
            "tag": "TST",
            "completed": ["A"],
            "date": "2006.1.1",
            "include_positions": True,
            "limit": 1,
            "offset": 1,
        },
    )

    assert result["ok"] is True, result
    assert result["scenario"] == {"tag": "TST", "completed": ["A"], "date": "2006.1.1"}
    assert result["collision_groups_total"] == 2
    assert result["collision_groups_returned"] == 1
    assert [focus["id"] for focus in result["collision_groups"][0]["focuses"]] == ["A", "B"]
    assert result["diagnostics_total"] >= 4
    assert result["unknown_condition_total"] == 1
    assert result["unresolved_chain_total"] == 3
    assert result["positions_are_partial"] is True
    assert result["evaluation"]["exact_engine_evaluation"] is False
    assert result["diagnostics_returned"] == 1
    assert result["diagnostics_truncated"] is True


def test_shim_refuses_invalid_scope_and_bad_pagination_before_parser(monkeypatch, tmp_path):
    root = tmp_path / "mod"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        upstream_analysis_shim,
        "_load_module",
        lambda *args, **kwargs: pytest.fail("upstream parser was loaded"),
    )
    base_payload = {
        "file": str(outside),
        "allowed_roots": [str(root)],
        "tag": "TST",
        "completed": [],
        "include_positions": False,
    }
    outside_result = upstream_analysis_shim._focus_overlap_scenarios(root, base_payload)
    assert outside_result["ok"] is False

    inside = root / "focus.txt"
    inside.write_text("fixture", encoding="utf-8")
    (root / "tools/analysis").mkdir(parents=True)
    (root / "tools/analysis/focus_overlap_report.py").write_text("stub", encoding="utf-8")
    bad_pagination = dict(base_payload, file=str(inside), limit="bad")
    result = upstream_analysis_shim._focus_overlap_scenarios(root, bad_pagination)
    assert result["ok"] is False
    assert "pagination" in result["error"]


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("relative", "absolute path"),
        ("roots", "allowed roots"),
        ("missing", "does not exist"),
        ("directory", "regular .txt"),
        ("extension", "regular .txt"),
        ("oversized", "2 MB"),
        ("tag", "country tag"),
        ("completed", "list of focus ids"),
        ("positions", "boolean"),
        ("pagination", "pagination"),
        ("helper", "no focus overlap analysis helper"),
    ],
)
def test_shim_refuses_invalid_payload_before_loading_upstream(monkeypatch, tmp_path, case, message):
    root = tmp_path / "mod"
    root.mkdir()
    source = root / "focus.txt"
    source.write_text("placeholder", encoding="utf-8")
    payload = {
        "file": str(source),
        "allowed_roots": [str(root)],
        "tag": "TST",
        "completed": [],
        "include_positions": False,
    }
    if case == "relative":
        payload["file"] = "focus.txt"
    elif case == "roots":
        payload["allowed_roots"] = str(root)
    elif case == "missing":
        payload["file"] = str(root / "missing.txt")
    elif case == "directory":
        payload["file"] = str(root)
    elif case == "extension":
        payload["file"] = str(root / "notes.yml")
        Path(payload["file"]).write_text("placeholder", encoding="utf-8")
    elif case == "oversized":
        source.write_bytes(b"x" * (2_000_001))
    elif case == "tag":
        payload["tag"] = "TOO-LONG"
    elif case == "completed":
        payload["completed"] = [1]
    elif case == "positions":
        payload["include_positions"] = "false"
    elif case == "pagination":
        payload["limit"] = "not-an-int"
    elif case == "helper":
        (root / "tools/analysis").mkdir(parents=True)
    monkeypatch.setattr(
        upstream_analysis_shim,
        "_load_module",
        lambda *args, **kwargs: pytest.fail("invalid payload must be refused before import"),
    )
    result = upstream_analysis_shim._focus_overlap_scenarios(root, payload)
    assert result["ok"] is False
    assert message in result["error"]


@pytest.mark.parametrize("value", ["../outside.txt", "common/national_focus", "wrong.yml"])
def test_wrapper_refuses_non_focus_paths_without_scanning(monkeypatch, fake_mod_root, value):
    monkeypatch.setattr(
        upstream_analysis, "_run_shim", lambda *args, **kwargs: pytest.fail("shim called")
    )
    result = upstream_analysis.focus_overlap_scenarios_tool(fake_mod_root, value, "TST")
    assert result["ok"] is False


def test_wrapper_refuses_symlink_and_outside_paths(monkeypatch, fake_mod_root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("focus = { id = outside }", encoding="utf-8")
    link = fake_mod_root / "common/national_focus/link.txt"
    link.symlink_to(outside)
    monkeypatch.setattr(
        upstream_analysis, "_run_shim", lambda *args, **kwargs: pytest.fail("shim called")
    )
    assert not upstream_analysis.focus_overlap_scenarios_tool(fake_mod_root, str(outside), "TST")[
        "ok"
    ]
    assert not upstream_analysis.focus_overlap_scenarios_tool(
        fake_mod_root, "common/national_focus/link.txt", "TST"
    )["ok"]


def test_wrapper_refuses_invalid_arguments_roots_and_oversized_source(
    monkeypatch, fake_mod_root, tmp_path
):
    source = fake_mod_root / "common/national_focus/oversized.txt"
    source.write_bytes(b"x" * 2_000_001)
    monkeypatch.setattr(
        upstream_analysis, "_run_shim", lambda *args, **kwargs: pytest.fail("shim called")
    )
    assert not upstream_analysis.focus_overlap_scenarios_tool(
        fake_mod_root, "common/national_focus/test.txt", "TST", limit="bad"
    )["ok"]
    assert not upstream_analysis.focus_overlap_scenarios_tool(fake_mod_root, "", "TST")["ok"]
    assert not upstream_analysis.focus_overlap_scenarios_tool(
        fake_mod_root,
        "common/national_focus/test.txt",
        "TST",
        include_positions="yes",
    )["ok"]
    assert not upstream_analysis.focus_overlap_scenarios_tool(
        fake_mod_root,
        "common/national_focus/test.txt",
        "TST",
        date=object(),
    )["ok"]
    assert not upstream_analysis.focus_overlap_scenarios_tool(
        tmp_path / "missing-root", "common/national_focus/test.txt", "TST"
    )["ok"]
    assert not upstream_analysis.focus_overlap_scenarios_tool(
        fake_mod_root, "common/national_focus/oversized.txt", "TST"
    )["ok"]


def test_wrapper_validates_scenario_and_forwards_explicit_scope(monkeypatch, fake_mod_root):
    calls = []
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda root, operation, payload, *, timeout: calls.append(
            (root, operation, payload, timeout)
        )
        or {"ok": True},
    )
    bad_tag = upstream_analysis.focus_overlap_scenarios_tool(
        fake_mod_root, "common/national_focus/test.txt", ""
    )
    bad_complete = upstream_analysis.focus_overlap_scenarios_tool(
        fake_mod_root,
        "common/national_focus/test.txt",
        "TST",
        completed=[1],
    )
    assert not bad_tag["ok"] and not bad_complete["ok"]
    good = upstream_analysis.focus_overlap_scenarios_tool(
        fake_mod_root,
        "common/national_focus/test.txt",
        "tst",
        completed=["TST_root"],
        date="1936.1.1",
        include_positions=True,
        limit=150,
        offset=2,
    )
    assert good["ok"] is True
    root, operation, payload, timeout = calls[0]
    assert root == fake_mod_root
    assert operation == "focus_overlap_scenarios"
    assert payload["tag"] == "TST"
    assert payload["completed"] == ["TST_root"]
    assert payload["date"] == "1936.1.1"
    assert payload["limit"] == 100 and payload["offset"] == 2
    assert timeout > 0


def test_wrapper_accepts_only_explicit_file_from_configured_submod(
    monkeypatch, fake_mod_root, tmp_path
):
    submod = tmp_path / "submod"
    focus_file = submod / "common/national_focus/overlay.txt"
    focus_file.parent.mkdir(parents=True)
    focus_file.write_text("focus = { id = OVERLAY_focus }", encoding="utf-8")
    mod_file = fake_mod_root / "common/national_focus/overlay.txt"
    mod_file.write_text("focus = { id = BASE_focus }", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda root, operation, payload, *, timeout: calls.append(payload) or {"ok": True},
    )

    result = upstream_analysis.focus_overlap_scenarios_tool(
        fake_mod_root,
        "common/national_focus/overlay.txt",
        "OVR",
        submod_root=submod,
    )

    assert result["ok"] is True
    assert calls[0]["file"] == str(focus_file.resolve())
    assert calls[0]["allowed_roots"] == [str(submod), str(fake_mod_root)]


@pytest.mark.parametrize(
    "malformed",
    [
        "focus = { id = A x = 0 y = 0",
        "focus_tree = { focus = { id = A x = 0 y = 0 } focus = { id = B x = 0 y = 0 }",
    ],
)
def test_shim_rejects_unbalanced_paradox_before_permissive_focus_parser(
    monkeypatch, tmp_path, malformed
):
    root = tmp_path / "mod"
    source = root / "focus.txt"
    source.parent.mkdir(parents=True)
    source.write_text(malformed, encoding="utf-8")
    module = _fake_api(monkeypatch, root)
    module.parse_focus_file = lambda text: pytest.fail("permissive parser must not run")
    result = upstream_analysis_shim._focus_overlap_scenarios(
        root,
        {
            "file": str(source),
            "allowed_roots": [str(root)],
            "tag": "TST",
            "completed": [],
            "include_positions": False,
        },
    )
    assert result["ok"] is False
    assert "malformed Paradox script" in result["error"]


def test_wrapper_budget_drop_marks_page_shortened(monkeypatch, fake_mod_root):
    focus_file = "common/national_focus/test.txt"
    huge = "x" * 100_000
    monkeypatch.setattr(
        upstream_analysis,
        "_run_shim",
        lambda *args, **kwargs: {
            "ok": True,
            "collision_groups_total": 1,
            "collision_groups_returned": 1,
            "collision_groups_truncated": False,
            "collision_groups": [huge],
            "positions_total": 1,
            "positions_returned": 1,
            "positions_truncated": False,
            "positions": [],
            "diagnostics_total": 0,
            "diagnostics_returned": 0,
            "diagnostics_truncated": False,
            "diagnostics": [],
        },
    )
    result = upstream_analysis.focus_overlap_scenarios_tool(fake_mod_root, focus_file, "TST")
    assert result["size_truncated"] is True
    assert result["collision_groups_returned"] == 0
    assert result["collision_groups_truncated"] is True


def test_shim_returns_structured_unsupported_and_strict_read_errors(monkeypatch, tmp_path):
    root = tmp_path / "mod"
    tool_dir = root / "tools/analysis"
    tool_dir.mkdir(parents=True)
    helper = tool_dir / "focus_overlap_report.py"
    helper.write_text("stub", encoding="utf-8")
    source = root / "focus.txt"
    source.write_bytes(b"\xff")
    payload = {
        "file": str(source),
        "allowed_roots": [str(root)],
        "tag": "TST",
        "completed": [],
        "include_positions": False,
    }
    monkeypatch.setattr(
        upstream_analysis_shim,
        "_load_module",
        lambda path, name: types.SimpleNamespace()
        if name.endswith("focus_overlap_report")
        else None,
    )
    missing = upstream_analysis_shim._focus_overlap_scenarios(root, payload)
    assert missing["ok"] is False and missing["unsupported"] is True

    _fake_api(monkeypatch, root)
    unreadable = upstream_analysis_shim._focus_overlap_scenarios(root, payload)
    assert unreadable["ok"] is False
    assert "Focus analysis failed" in unreadable["error"]


def test_shim_rejects_invalid_explicit_date(monkeypatch, tmp_path):
    root = tmp_path / "mod"
    source = root / "focus.txt"
    source.parent.mkdir(parents=True)
    source.write_text("focus_tree = { focus = { id = A x = 0 y = 0 } }", encoding="utf-8")
    _fake_api(monkeypatch, root)
    result = upstream_analysis_shim._focus_overlap_scenarios(
        root,
        {
            "file": str(source),
            "allowed_roots": [str(root)],
            "tag": "TST",
            "completed": [],
            "date": "",
            "include_positions": False,
        },
    )
    assert result["ok"] is False
    assert "Focus analysis failed" in result["error"]


@pytest.mark.parametrize("value", ["2006.13.1", "2006.2.30", "0000.1.1", "2001.2.29"])
def test_shim_rejects_impossible_calendar_dates(monkeypatch, tmp_path, value):
    root = tmp_path / "mod"
    source = root / "focus.txt"
    source.parent.mkdir(parents=True)
    source.write_text("focus = { id = A x = 0 y = 0 }", encoding="utf-8")
    _fake_api(monkeypatch, root)
    result = upstream_analysis_shim._focus_overlap_scenarios(
        root,
        {
            "file": str(source),
            "allowed_roots": [str(root)],
            "tag": "TST",
            "completed": [],
            "date": value,
            "include_positions": False,
        },
    )
    assert result["ok"] is False
    assert "valid calendar date" in result["error"]


@pytest.mark.parametrize("value", ["2004.2.29", "2001.2.28"])
def test_shim_accepts_leap_and_nonleap_calendar_edges(monkeypatch, tmp_path, value):
    root = tmp_path / "mod"
    source = root / "focus.txt"
    source.parent.mkdir(parents=True)
    source.write_text("focus_tree = { focus = { id = A x = 0 y = 0 } }", encoding="utf-8")
    _fake_api(monkeypatch, root)
    result = upstream_analysis_shim._focus_overlap_scenarios(
        root,
        {
            "file": str(source),
            "allowed_roots": [str(root)],
            "tag": "TST",
            "completed": [],
            "date": value,
            "include_positions": False,
        },
    )
    assert result["ok"] is True
    assert result["scenario"]["date"] == value


@pytest.mark.parametrize(
    "mod_root",
    [Path("/workspace/Millennium-Dawn"), Path("/workspace/scratch/md-upstream-fb657ff7")],
)
def test_real_poland_report_matches_upstream_helper(mod_root: Path, tmp_path: Path):
    focus_path = mod_root / "common/national_focus/05_poland.txt"
    helper = mod_root / "tools/analysis/focus_overlap_report.py"
    if not focus_path.is_file() or not helper.is_file():
        pytest.skip(f"upstream checkout unavailable: {mod_root}")
    before = focus_path.stat().st_mtime_ns, focus_path.stat().st_size
    result = upstream_analysis.focus_overlap_scenarios_tool(
        mod_root,
        "common/national_focus/05_poland.txt",
        "POL",
        completed=["POL_stay_on_track"],
        date="2006.1.1",
        include_positions=True,
        limit=50,
    )
    assert result["ok"] is True, result
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    direct = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--file",
            str(focus_path),
            "--tag",
            "POL",
            "--completed",
            "POL_stay_on_track",
            "--date",
            "2006.1.1",
            "--map",
            "--format",
            "json",
        ],
        cwd=mod_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    expected = json.loads(direct.stdout)
    assert result["focus_total"] == expected["focuses"]
    assert result["visible_total"] == expected["visible"]
    assert result["collision_groups"] == expected["collisions"][:50]
    assert result["positions"] == expected["map"][:50]
    assert result["scenario"] == expected["scenario"]
    assert (focus_path.stat().st_mtime_ns, focus_path.stat().st_size) == before


@pytest.mark.parametrize(
    "upstream_root",
    [Path("/workspace/Millennium-Dawn"), Path("/workspace/scratch/md-upstream-fb657ff7")],
)
def test_real_upstream_helper_refuses_quoted_focus_ids(upstream_root: Path, tmp_path: Path):
    upstream_script = upstream_root / "tools/analysis/focus_overlap_report.py"
    upstream_shared = upstream_root / "tools/shared_utils.py"
    if not upstream_script.is_file() or not upstream_shared.is_file():
        pytest.skip(f"upstream helper unavailable: {upstream_root}")
    mod_root = tmp_path / "mod"
    script_dir = mod_root / "tools/analysis"
    script_dir.mkdir(parents=True)
    (mod_root / "tools/shared_utils.py").parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(upstream_script, script_dir / upstream_script.name)
    shutil.copyfile(upstream_shared, mod_root / "tools/shared_utils.py")
    source = mod_root / "focus.txt"
    source.write_text(
        "focus_tree = {\n"
        ' focus = { id = "A" x = 0 y = 0 }\n'
        ' focus = { id = "B" x = 0 y = 0 }\n'
        "}\n",
        encoding="utf-8",
    )

    result = upstream_analysis.focus_overlap_scenarios_tool(mod_root, "focus.txt", "TST")

    assert result["ok"] is False
    assert "Quoted focus ids" in result["error"]


@pytest.mark.parametrize(
    "upstream_root",
    [Path("/workspace/Millennium-Dawn"), Path("/workspace/scratch/md-upstream-fb657ff7")],
)
def test_real_upstream_helper_refuses_compact_focus_definitions(
    upstream_root: Path, tmp_path: Path
):
    upstream_script = upstream_root / "tools/analysis/focus_overlap_report.py"
    upstream_shared = upstream_root / "tools/shared_utils.py"
    if not upstream_script.is_file() or not upstream_shared.is_file():
        pytest.skip(f"upstream helper unavailable: {upstream_root}")
    mod_root = tmp_path / "mod"
    script_dir = mod_root / "tools/analysis"
    script_dir.mkdir(parents=True)
    shutil.copyfile(upstream_script, script_dir / upstream_script.name)
    shutil.copyfile(upstream_shared, mod_root / "tools/shared_utils.py")
    source = mod_root / "focus.txt"
    source.write_text(
        "focus_tree = { focus = { id = A x = 0 y = 0 } focus = { id = B x = 0 y = 0 } }\n",
        encoding="utf-8",
    )

    result = upstream_analysis.focus_overlap_scenarios_tool(mod_root, "focus.txt", "TST")

    assert result["ok"] is False
    assert "did not recognize the focus definitions" in result["error"]
