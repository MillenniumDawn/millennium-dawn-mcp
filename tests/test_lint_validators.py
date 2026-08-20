"""Tests for the lint -> validator bridge (`validators=` param on lint_tool)."""

from __future__ import annotations

import inspect
from pathlib import Path

# pi-lens-ignore: reportMissingImports
import pytest

from md_mcp.tools.lint_validators import (
    SCAN_PREFIXES,
    UNATTRIBUTED_SAMPLE,
    VALIDATOR_AUTO_MAP,
    run_validators_for_lint,
    select_validators,
)
from md_mcp.tools.linting_tools import lint_tool
from md_mcp.validators import SLOW_VALIDATORS, ValidatorInfo, ValidatorRunner, available_validators

from .test_lint_dispatcher import _git, _init_repo, _seed_all_scripts

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


@pytest.fixture
def mod_with_files(tmp_path):
    """Mod tree the attributor can resolve partial paths and message tokens against."""
    for rel in (
        "events/Algeria.txt",
        "events/Brazil.txt",
        "localisation/english/MD_focus_AST_l_english.yml",
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    return tmp_path


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


# Broad rows cover the validators whose source scan globs reach the whole
# common/, events/, and/or history/ tree (see VALIDATOR_AUTO_MAP docstring);
# narrow rows add the domain-specific pass for that directory.
_BROAD_COMMON = {
    "agency_upgrades",
    "events",
    "file_paths",
    "gfx_references",
    "ideas",
    "scripted_gui",
    "simplifications",
}
# Same for history/: the common/ broad set minus simplifications, which only
# reaches common/ and events/ upstream.
_BROAD_HISTORY = {"agency_upgrades", "events", "file_paths", "gfx_references", "history", "ideas"}


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "common/national_focus/USA.txt",
            _BROAD_COMMON | {"focus_tree", "modifiers", "oob_units", "scripted_params", "style"},
        ),
        (
            "events/Afghanistan.txt",
            _BROAD_COMMON | {"focus_tree", "on_actions", "oob_units", "scripted_params", "style"},
        ),
        (
            "common/decisions/USA.txt",
            _BROAD_COMMON | {"decisions", "modifiers", "oob_units", "scripted_params", "style"},
        ),
        ("common/ideas/USA.txt", _BROAD_COMMON | {"history", "modifiers", "style"}),
        ("common/characters/USA.txt", _BROAD_COMMON | {"style"}),
        ("common/country_leader/USA.txt", _BROAD_COMMON | {"style"}),
        ("common/modifiers/USA.txt", _BROAD_COMMON | {"style"}),
        ("common/opinion_modifiers/USA.txt", _BROAD_COMMON | {"style"}),
        ("common/dynamic_modifiers/USA.txt", _BROAD_COMMON | {"modifiers", "style"}),
        ("common/modifier_definitions/USA.txt", _BROAD_COMMON | {"modifiers", "style"}),
        ("common/scripted_guis/x.txt", _BROAD_COMMON | {"style"}),
        ("common/ai_strategy/USA.txt", _BROAD_COMMON | {"ai_roles", "style"}),
        (
            "common/units/inf.txt",
            _BROAD_COMMON | {"ai_equipment", "ai_navy", "modifiers", "oob_units", "style"},
        ),
        ("history/units/USA_2000.txt", _BROAD_HISTORY | {"oob_units", "style"}),
        ("history/countries/USA.txt", _BROAD_HISTORY | {"oob_units", "style"}),
        (
            "interface/usa.gfx",
            {
                "agency_upgrades",
                "factions",
                "file_paths",
                "gfx_references",
                "ideas",
                "scripted_gui",
                "scripted_localisation",
            },
        ),
        (
            "localisation/english/MD_focus_USA_l_english.yml",
            {"file_paths", "gfx_references", "localisation", "scripted_gui"},
        ),
        ("descriptor.mod", {"mod_descriptors"}),
    ],
)
def test_select_validators_mapping(path, expected):
    assert set(select_validators([path], set(_ALL_NAMES))) == expected


@pytest.mark.parametrize(
    "path",
    [
        "common/characters/USA.txt",
        "common/country_leader/USA.txt",
        "common/modifiers/USA.txt",
        "common/opinion_modifiers/USA.txt",
    ],
)
def test_select_validators_does_not_route_modifiers_outside_scan_domain(path):
    assert "modifiers" not in select_validators([path], set(_ALL_NAMES))


def test_select_validators_dedups_across_files():
    got = select_validators(["common/ideas/USA.txt", "common/ideas/CAN.txt"], set(_ALL_NAMES))
    assert got == sorted(_BROAD_COMMON | {"history", "modifiers", "style"})


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


def test_run_validators_attributes_fileless_issues_from_the_message(mod_with_files):
    # `validate_events` emits `f"{eid} - {filename}"`, which upstream's location
    # regexes don't match, so `file` lands empty. The filename is still in the
    # message: recover it, then scope on the real path.
    runner = FakeRunner(
        results={
            "events": {
                "ok": True,
                "issues": [
                    _issue("", message="ALG.2001 - Algeria.txt"),
                    _issue("", message="BRA.1 - Brazil.txt"),
                ],
            }
        }
    )
    entries, issues = run_validators_for_lint(
        runner,
        ["events"],
        staged_only=False,
        relevant_set={"events/Algeria.txt"},
        mod_root=mod_with_files,
    )
    assert entries == [{"name": "validator:events", "ok": True, "total": 1, "total_mod_wide": 2}]
    assert [i["file"] for i in issues] == ["events/Algeria.txt"]


def test_run_validators_resolves_basename_only_issues(mod_with_files):
    # validate_localisation.py:92,102 emits os.path.basename(filename). Exact
    # matching dropped these outright: the drop half of the scoping bug.
    runner = FakeRunner(
        results={
            "localisation": {
                "ok": True,
                "issues": [_issue("MD_focus_AST_l_english.yml", line=2, category="mangled")],
            }
        }
    )
    entries, issues = run_validators_for_lint(
        runner,
        ["localisation"],
        staged_only=False,
        relevant_set={"localisation/english/MD_focus_AST_l_english.yml"},
        mod_root=mod_with_files,
    )
    assert entries[0]["total"] == 1
    assert issues[0]["file"] == "localisation/english/MD_focus_AST_l_english.yml"
    assert issues[0]["line"] == 2


def test_run_validators_caps_unattributable_issues(mod_with_files):
    # The flood half: 762 fileless `events` issues used to merge wholesale at
    # 137 KB regardless of scope. Report the count, sample the detail.
    many = [_issue("", message=f"{n} orphaned keys") for n in range(50)]
    runner = FakeRunner(results={"events": {"ok": True, "issues": many}})
    entries, issues = run_validators_for_lint(
        runner,
        ["events"],
        staged_only=False,
        relevant_set={"events/Algeria.txt"},
        mod_root=mod_with_files,
    )
    assert entries[0]["total"] == 0
    assert entries[0]["unattributed"] == 50
    assert len(issues) == UNATTRIBUTED_SAMPLE
    assert all(i["scope"] == "unattributed" for i in issues)


def test_run_validators_drops_off_scope_issues_resolved_from_message(mod_with_files):
    runner = FakeRunner(
        results={"events": {"ok": True, "issues": [_issue("", message="BRA.1 - Brazil.txt")]}}
    )
    entries, issues = run_validators_for_lint(
        runner,
        ["events"],
        staged_only=False,
        relevant_set={"events/Algeria.txt"},
        mod_root=mod_with_files,
    )
    assert entries[0]["total"] == 0
    assert "unattributed" not in entries[0]
    assert issues == []


def test_run_validators_total_matches_issues_returned(mod_with_files):
    runner = FakeRunner(
        results={
            "events": {
                "ok": True,
                "issues": [
                    _issue("", message="ALG.1 - Algeria.txt"),
                    _issue("", message="ALG.2 - Algeria.txt"),
                    _issue("", message="BRA.1 - Brazil.txt"),
                ],
            }
        }
    )
    entries, issues = run_validators_for_lint(
        runner,
        ["events"],
        staged_only=False,
        relevant_set={"events/Algeria.txt"},
        mod_root=mod_with_files,
    )
    assert entries[0]["total"] == len(issues) == 2


def test_run_validators_fileless_not_marked_without_scope(mod_with_files):
    runner = FakeRunner(results={"events": {"ok": True, "issues": [_issue("", message="x")]}})
    entries, issues = run_validators_for_lint(
        runner, ["events"], staged_only=False, relevant_set=None, mod_root=mod_with_files
    )
    assert "unattributed" not in entries[0]
    assert "scope" not in issues[0]


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


@pytest.mark.parametrize(
    "mode,staged_only",
    [("changed", False), ("staged", True), ("all", False)],
)
def test_lint_default_runs_style_validator_for_script_scope(tmp_path, mode, staged_only):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    (tmp_path / "descriptor.mod").write_text('name = "x"\n')
    script_file = tmp_path / "common" / "x.txt"
    script_file.parent.mkdir()
    script_file.write_text("x = 1\n")
    if mode == "staged":
        _git(tmp_path, "add", "common/x.txt")

    runner = FakeRunner(names=["style"])
    out = lint_tool(tmp_path, mode=mode, validator_runner=runner)

    assert out["ok"] is True
    assert out["validators_run"] == ["style"]
    assert out["failed_checks"] == []
    assert runner.calls == [{"name": "style", "staged_only": staged_only}]
    assert any(c["name"] == "validator:style" for c in out["checks"])


def test_lint_default_clean_tree_runs_no_validators(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    _git(tmp_path, "add", "tools")
    _git(tmp_path, "commit", "-qm", "add lint fixtures")
    runner = FakeRunner(names=["style"])

    out = lint_tool(tmp_path, validator_runner=runner)

    assert out["ok"] is True
    assert out["validators_run"] == []
    assert runner.calls == []
    assert not [c for c in out["checks"] if c["name"].startswith("validator:")]


def test_lint_default_non_script_scope_runs_no_validators(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    (tmp_path / "descriptor.mod").write_text('name = "x"\n')
    runner = FakeRunner(names=["style"])

    out = lint_tool(tmp_path, files=["descriptor.mod"], validator_runner=runner)

    assert out["ok"] is True
    assert out["validators_run"] == []
    assert runner.calls == []
    assert not [c for c in out["checks"] if c["name"].startswith("validator:")]


def test_lint_explicit_empty_validators_disables_validator_runs(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    runner = FakeRunner(names=["style"])
    out = lint_tool(tmp_path, validators=[], validator_runner=runner)
    assert out["ok"] is True
    assert out["validators_run"] == []
    assert runner.calls == []
    assert not [c for c in out["checks"] if c["name"].startswith("validator:")]


def test_lint_unavailable_default_style_is_a_failed_check(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    script_file = tmp_path / "events" / "x.txt"
    script_file.parent.mkdir()
    script_file.write_text("x = 1\n")
    runner = FakeRunner(names=[])
    out = lint_tool(tmp_path, validator_runner=runner)
    assert out["ok"] is False
    # Nothing ran: the setup failure blocks the run, and validators_run says so.
    assert out["validators_run"] == []
    assert out["failed_checks"] == ["validator:style"]
    assert runner.calls == []
    style = next(c for c in out["checks"] if c["name"] == "validator:style")
    assert style["ok"] is False
    assert "unavailable" in style["error"]
    assert all(c["ok"] for c in out["checks"] if not c["name"].startswith("validator:"))


def test_lint_requested_validator_failure_sets_top_level_failure(tmp_path):
    _init_repo(tmp_path)
    _seed_all_scripts(tmp_path, {})
    runner = FakeRunner(names=["history"], results={"history": {"ok": False, "error": "boom"}})
    out = lint_tool(tmp_path, validators=["history"], validator_runner=runner)
    assert out["ok"] is False
    assert out["failed_checks"] == ["validator:history"]
    assert all(c["ok"] for c in out["checks"] if not c["name"].startswith("validator:"))


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


@pytest.mark.integration
def test_auto_map_validator_names_exist_upstream(real_mod_root):
    # SCAN_PREFIXES is the full universe select_validators can return for
    # validators=["auto"]. A silent upstream rename would drop a name from
    # here without failing anything else — catch that drift explicitly.
    available = {v.name for v in available_validators(real_mod_root)}
    missing = set(SCAN_PREFIXES) - available
    assert not missing, f"auto-routable validator names missing upstream: {sorted(missing)}"
