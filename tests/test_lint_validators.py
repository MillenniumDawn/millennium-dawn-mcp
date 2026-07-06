"""Tests for the lint -> validator bridge (`validators=` param on lint_tool)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from md_mcp.tools.lint_validators import (
    VALIDATOR_AUTO_MAP,
    run_validators_for_lint,
    select_validators,
)
from md_mcp.tools.linting_tools import lint_tool
from md_mcp.validators import SLOW_VALIDATORS, ValidatorInfo, ValidatorRunner

from .test_lint_dispatcher import _init_repo, _seed_all_scripts

_ALL_NAMES = sorted(
    {v for _, vals in VALIDATOR_AUTO_MAP for v in vals}
    | {"localisation", "style", "variables", "set_variables", "cosmetic_tags"}
    | set(SLOW_VALIDATORS)
)


class FakeRunner(ValidatorRunner):
    """Stands in for ValidatorRunner; records run() kwargs, returns canned results."""

    def __init__(self, results: dict | None = None, names: list | None = None):
        super().__init__(Path("/nonexistent"))
        self.results = results or {}
        self.names = names if names is not None else _ALL_NAMES
        self.calls: list = []

    def list(self):
        return [
            ValidatorInfo(name=n, module_name=f"validate_{n}", title=n, path=Path(f"/x/{n}.py"))
            for n in self.names
        ]

    def run(self, name, *, staged_only=False, files=None):
        self.calls.append({"name": name, "staged_only": staged_only})
        if name in self.results:
            return self.results[name]
        return {"ok": True, "validator": name, "counts": {}, "issues": []}


def _issue(file, message="bad", severity="warning", line=0, category="CAT"):
    return {
        "file": file,
        "message": message,
        "severity": severity,
        "line": line,
        "category": category,
    }


# ---------------------------------------------------------------------------
# select_validators mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "common/national_focus/USA.txt",
            {"focus_tree", "scripted_params", "simplifications", "modifiers", "style"},
        ),
        (
            "events/Afghanistan.txt",
            {"events", "on_actions", "scripted_params", "simplifications", "scripted_gui", "style"},
        ),
        (
            "common/decisions/USA.txt",
            {"decisions", "scripted_params", "simplifications", "modifiers", "style"},
        ),
        ("common/ideas/USA.txt", {"ideas", "modifiers", "style"}),
        ("common/scripted_guis/x.txt", {"scripted_gui", "gfx_references", "style"}),
        ("common/ai_strategy/USA.txt", {"ai_roles", "style"}),
        ("common/units/inf.txt", {"ai_navy", "oob_units", "style"}),
        ("history/units/USA_2000.txt", {"oob_units", "history", "style"}),
        ("history/countries/USA.txt", {"history", "style"}),
        ("interface/usa.gfx", {"gfx_references", "scripted_gui"}),
        ("localisation/english/MD_focus_USA_l_english.yml", {"localisation"}),
        ("descriptor.mod", set()),
    ],
)
def test_select_validators_mapping(path, expected):
    assert set(select_validators([path], set(_ALL_NAMES))) == expected


def test_select_validators_dedups_across_files():
    got = select_validators(["common/ideas/USA.txt", "common/ideas/CAN.txt"], set(_ALL_NAMES))
    assert got == sorted({"ideas", "modifiers", "style"})


def test_select_validators_mode_all_is_fast_set():
    got = select_validators(None, set(_ALL_NAMES))
    assert got == sorted(set(_ALL_NAMES) - SLOW_VALIDATORS)


def test_select_validators_empty_scope_selects_nothing():
    assert select_validators([], set(_ALL_NAMES)) == []


def test_select_validators_never_auto_selects_globals():
    paths = [
        "common/national_focus/USA.txt",
        "events/X.txt",
        "history/countries/USA.txt",
        "localisation/english/x_l_english.yml",
        "interface/x.gfx",
    ]
    got = set(select_validators(paths, set(_ALL_NAMES)))
    assert not got & {"variables", "set_variables", "cosmetic_tags"}
    assert not got & SLOW_VALIDATORS


def test_select_validators_intersects_available():
    got = select_validators(["common/national_focus/USA.txt"], {"focus_tree"})
    assert got == ["focus_tree"]


# ---------------------------------------------------------------------------
# run_validators_for_lint
# ---------------------------------------------------------------------------


def test_run_validators_scopes_and_reports_mod_wide():
    runner = FakeRunner(
        results={
            "focus_tree": {
                "ok": True,
                "issues": [
                    _issue("common/national_focus/USA.txt", line=42),
                    _issue("common/national_focus/OTHER.txt"),
                ],
            }
        }
    )
    entries, issues = run_validators_for_lint(
        runner,
        ["focus_tree"],
        staged_only=False,
        relevant_set={"common/national_focus/USA.txt"},
    )
    assert entries == [
        {"name": "validator:focus_tree", "ok": True, "total": 1, "total_mod_wide": 2}
    ]
    assert len(issues) == 1
    assert issues[0]["check"] == "validator:focus_tree"
    assert issues[0]["file"] == "common/national_focus/USA.txt"
    assert issues[0]["line"] == 42
    assert issues[0]["category"] == "CAT"


def test_run_validators_omits_falsy_line():
    runner = FakeRunner(results={"history": {"ok": True, "issues": [_issue("history/a.txt")]}})
    _, issues = run_validators_for_lint(runner, ["history"], staged_only=False, relevant_set=None)
    assert "line" not in issues[0]


def test_run_validators_failure_isolated():
    runner = FakeRunner(
        results={
            "focus_tree": {"ok": False, "error": "import blew up"},
            "history": {"ok": True, "issues": [_issue("history/a.txt")]},
        }
    )
    entries, issues = run_validators_for_lint(
        runner, ["focus_tree", "history"], staged_only=False, relevant_set=None
    )
    assert entries[0] == {"name": "validator:focus_tree", "ok": False, "error": "import blew up"}
    assert entries[1]["ok"] is True
    assert len(issues) == 1


def test_run_validators_no_scope_no_mod_wide_key():
    runner = FakeRunner(results={"history": {"ok": True, "issues": [_issue("history/a.txt")]}})
    entries, _ = run_validators_for_lint(runner, ["history"], staged_only=False, relevant_set=None)
    assert "total_mod_wide" not in entries[0]


# ---------------------------------------------------------------------------
# lint_tool integration (stub scripts + FakeRunner)
# ---------------------------------------------------------------------------


def test_lint_signature_has_validator_params():
    params = inspect.signature(lint_tool).parameters
    assert "validators" in params
    assert "validator_runner" in params
    assert params["validators"].default is None
    assert params["validator_runner"].default is None


def test_lint_default_runs_no_validators(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    runner = FakeRunner()
    out = lint_tool(tmp_path, validator_runner=runner)
    assert out["ok"] is True
    assert "validators_run" not in out
    assert runner.calls == []
    assert not [c for c in out["checks"] if c["name"].startswith("validator:")]


def test_lint_validators_auto_merges_and_scopes(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    changed = tmp_path / "common" / "national_focus" / "USA.txt"
    changed.parent.mkdir(parents=True)
    changed.write_text("focus_tree = {}\n")

    runner = FakeRunner(
        names=["focus_tree", "modifiers", "scripted_params", "simplifications", "style"],
        results={
            "focus_tree": {
                "ok": True,
                "issues": [
                    _issue("common/national_focus/USA.txt", line=7),
                    _issue("common/national_focus/UNTOUCHED.txt"),
                ],
            }
        },
    )
    out = lint_tool(tmp_path, mode="changed", validators=["auto"], validator_runner=runner)
    assert out["validators_run"] == [
        "focus_tree",
        "modifiers",
        "scripted_params",
        "simplifications",
        "style",
    ]
    ft = next(c for c in out["checks"] if c["name"] == "validator:focus_tree")
    assert ft["total"] == 1
    assert ft["total_mod_wide"] == 2
    v_issues = [i for i in out["issues"] if i["check"] == "validator:focus_tree"]
    assert len(v_issues) == 1
    assert v_issues[0]["file"] == "common/national_focus/USA.txt"
    assert out["counts"]["warning"] >= 1


def test_lint_validators_explicit_union_with_auto(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    changed = tmp_path / "history" / "countries" / "USA.txt"
    changed.parent.mkdir(parents=True)
    changed.write_text("x = 1\n")

    runner = FakeRunner(names=["history", "style", "variables"])
    out = lint_tool(
        tmp_path, mode="changed", validators=["auto", "variables"], validator_runner=runner
    )
    assert out["validators_run"] == ["history", "style", "variables"]


def test_lint_validators_unknown_rejected(tmp_path):
    _init_repo(tmp_path)
    runner = FakeRunner(names=["focus_tree"])
    out = lint_tool(tmp_path, validators=["nonsense"], validator_runner=runner)
    assert out["ok"] is False
    assert "nonsense" in out["error"]
    assert "focus_tree" in out["error"]
    assert runner.calls == []


def test_lint_validators_star_excludes_slow(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    runner = FakeRunner(names=["focus_tree", "unused_scripted", "unused_textures"])
    out = lint_tool(tmp_path, validators=["*"], validator_runner=runner)
    assert out["validators_run"] == ["focus_tree"]


def test_lint_validators_clean_tree_auto_selects_nothing(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    runner = FakeRunner()
    out = lint_tool(tmp_path, mode="changed", validators=["auto"], validator_runner=runner)
    # _seed_all_scripts creates untracked stub scripts under tools/, which map
    # to no validator domain.
    assert out["validators_run"] == []
    assert runner.calls == []


def test_lint_validators_counts_only(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    runner = FakeRunner(
        names=["history"],
        results={"history": {"ok": True, "issues": [_issue("history/a.txt")]}},
    )
    out = lint_tool(tmp_path, validators=["history"], counts_only=True, validator_runner=runner)
    assert "issues" not in out
    assert any(c["name"] == "validator:history" for c in out["checks"])


def test_lint_validators_truncation(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    many = [_issue(f"history/{n}.txt") for n in range(20)]
    runner = FakeRunner(names=["history"], results={"history": {"ok": True, "issues": many}})
    out = lint_tool(tmp_path, mode="all", validators=["history"], limit=5, validator_runner=runner)
    assert out["truncated"] is True
    assert out["issues_total_after_filter"] == 20
    assert len(out["issues"]) == 5


def test_lint_staged_mode_passes_staged_only(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    staged = tmp_path / "history" / "countries" / "USA.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("x = 1\n")
    import subprocess

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)

    runner = FakeRunner(names=["history", "style"])
    out = lint_tool(tmp_path, mode="staged", validators=["auto"], validator_runner=runner)
    assert out["ok"] is True
    assert runner.calls
    assert all(c["staged_only"] is True for c in runner.calls)


def test_lint_validators_severity_floor_applies(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    runner = FakeRunner(
        names=["history"],
        results={
            "history": {
                "ok": True,
                "issues": [
                    _issue("history/a.txt", severity="warning"),
                    _issue("history/b.txt", severity="error"),
                ],
            }
        },
    )
    out = lint_tool(
        tmp_path, mode="all", validators=["history"], severity_min="error", validator_runner=runner
    )
    assert out["issues_total_after_filter"] == 1
    assert out["issues"][0]["severity"] == "error"
    # counts stay pre-filter, same as lint checks
    assert out["counts"]["warning"] == 1
    assert out["counts"]["error"] == 1


# ---------------------------------------------------------------------------
# integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_lint_with_real_validator(real_mod_root, tmp_path):
    from md_mcp.validators import ValidatorRunner

    runner = ValidatorRunner(real_mod_root)
    out = lint_tool(
        real_mod_root,
        mode="all",
        checks=["mod_encoding"],
        validators=["cosmetic_tags"],
        counts_only=True,
        validator_runner=runner,
    )
    assert out["ok"] is True
    assert out["validators_run"] == ["cosmetic_tags"]
    assert any(c["name"] == "validator:cosmetic_tags" for c in out["checks"])
