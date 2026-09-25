from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

run_validators_for_lint: Any = importlib.import_module(
    "md_mcp.tools.lint_validators"
).run_validators_for_lint
ValidatorRunner: Any = importlib.import_module("md_mcp.validators").ValidatorRunner

_ISSUE = """
class _Issue:
    def __init__(self, **values):
        self.values = values

    def to_dict(self):
        return dict(self.values)
"""

_SCOPED_VALIDATOR = (
    _ISSUE
    + """
from pathlib import Path


class Validator:
    def __init__(self, mod_path, use_colors=False, staged_only=False, **kwargs):
        self.mod_path = Path(mod_path)
        self.staged_only = staged_only
        self.staged_files = None
        self._issues = []

    def _collect_files(self, patterns, ignore_staged=False):
        files = sorted(self.mod_path.glob(patterns[0]))
        if self.staged_only and not ignore_staged:
            selected = set(self.staged_files or [])
            files = [
                path for path in files
                if path.relative_to(self.mod_path).as_posix() in selected
            ]
        return files

    def run_all_validations(self):
        definitions = {
            path.read_text(encoding="utf-8").strip()
            for path in self._collect_files(["common/ideas/*.txt"], ignore_staged=True)
        }
        for path in self._collect_files(["events/*.txt"]):
            reference = path.read_text(encoding="utf-8").strip()
            category = "reference" if reference in definitions else "missing-reference"
            message = f"checked {reference}" if reference in definitions else f"missing {reference}"
            self._issues.append(
                _Issue(
                    severity="warning",
                    category=category,
                    message=message,
                    file=path.relative_to(self.mod_path).as_posix(),
                )
            )
        self._issues.append(
            _Issue(severity="warning", category="orphan", message="unattributed", file="")
        )
"""
)

_LEGACY_VALIDATOR = (
    _ISSUE
    + """
from pathlib import Path


class Validator:
    def __init__(self, mod_path, use_colors=False, staged_only=False, **kwargs):
        self.mod_path = Path(mod_path)
        self.staged_only = staged_only
        self._issues = []

    def run_all_validations(self):
        files = sorted((self.mod_path / "events").glob("*.txt"))
        for path in files:
            self._issues.append(
                _Issue(
                    severity="warning",
                    category="legacy",
                    message=f"scanned={len(files)} staged={self.staged_only}",
                    file=path.relative_to(self.mod_path).as_posix(),
                )
            )
"""
)


def _write_fixture(root: Path, validator: str, source: str) -> None:
    (root / "tools" / "validation").mkdir(parents=True)
    (root / "events").mkdir()
    (root / "common" / "ideas").mkdir(parents=True)
    (root / "tools" / "validation" / f"validate_{validator}.py").write_text(
        source, encoding="utf-8"
    )
    (root / "events" / "Algeria.txt").write_text("idea_two", encoding="utf-8")
    (root / "events" / "Brazil.txt").write_text("unknown_idea", encoding="utf-8")
    (root / "common" / "ideas" / "CAN.txt").write_text("idea_two", encoding="utf-8")
    (root / "common" / "ideas" / "USA.txt").write_text("idea_one", encoding="utf-8")


def _assert_primary_scope(root: Path, mode: str) -> None:
    _write_fixture(root, "scope_fixture", _SCOPED_VALIDATOR)
    runner = ValidatorRunner(root, mode=mode)
    target = "events/Algeria.txt"
    run: Any = runner.run

    full = run("scope_fixture", post_filter=False)
    scoped = run("scope_fixture", files=[target], post_filter=False)

    assert "scoped" not in full
    assert scoped["scoped"] is True
    full_on_scope = [issue for issue in full["issues"] if issue.get("file") == target]
    scoped_primary = [issue for issue in scoped["issues"] if issue.get("file")]
    assert scoped_primary == full_on_scope
    assert scoped_primary == [
        {
            "severity": "warning",
            "category": "reference",
            "message": "checked idea_two",
            "file": target,
        }
    ]
    assert all(issue.get("file") != "events/Brazil.txt" for issue in scoped["issues"])


def test_scope_limits_primary_inputs_and_keeps_full_definition_lookups(tmp_path):
    for mode in ("isolated", "in_process"):
        _assert_primary_scope(tmp_path / mode / "Mod", mode)


def test_scope_rejects_paths_outside_the_mod_root(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_unsafe", _SCOPED_VALIDATOR)
    runner = ValidatorRunner(root, mode="in_process")
    run: Any = runner.run

    full = run("scope_unsafe", post_filter=False)
    unsafe = run("scope_unsafe", files=["../outside.txt"], post_filter=False)

    assert unsafe["issues"] == full["issues"]


def test_lint_post_filter_preserves_unattributed_issues(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_lint", _SCOPED_VALIDATOR)
    runner = ValidatorRunner(root)

    entries, issues = run_validators_for_lint(
        runner,
        ["scope_lint"],
        staged_only=False,
        relevant_set={"events/Algeria.txt"},
        mod_root=root,
    )

    assert entries == [
        {
            "name": "validator:scope_lint",
            "ok": True,
            "total": 1,
            "total_mod_wide": 2,
            "scoped": True,
            "unattributed": 1,
        }
    ]
    assert [issue["file"] for issue in issues] == ["events/Algeria.txt", ""]
    assert issues[-1]["scope"] == "unattributed"


def test_unsupported_collector_falls_back_to_full_scan_and_post_filter(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_legacy", _LEGACY_VALIDATOR)
    runner = ValidatorRunner(root)

    result = runner.run("scope_legacy", files=["events/Algeria.txt"])

    assert result["ok"] is True
    assert [issue["file"] for issue in result["issues"]] == ["events/Algeria.txt"]
    assert result["issues"][0]["message"] == "scanned=2 staged=False"
