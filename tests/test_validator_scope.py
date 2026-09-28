from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

run_validators_for_lint: Any = importlib.import_module(
    "md_mcp.tools.lint_validators"
).run_validators_for_lint
ValidatorRunner: Any = importlib.import_module("md_mcp.validators").ValidatorRunner
_shim: Any = importlib.import_module("md_mcp.validators._shim")

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

_STAGED_GUARD_VALIDATOR = (
    _ISSUE
    + """
from pathlib import Path

last_instance = None


class Validator:
    def __init__(self, mod_path, use_colors=False, staged_only=False, **kwargs):
        global last_instance
        self.mod_path = Path(mod_path)
        self.staged_only = staged_only
        self.staged_files = None
        self._issues = []
        last_instance = self

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
        if self.staged_only:
            return
        for path in self._collect_files(["events/*.txt"]):
            self._issues.append(
                _Issue(
                    severity="warning",
                    category="event",
                    message="checked",
                    file=path.relative_to(self.mod_path).as_posix(),
                )
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


def test_scoped_collect_does_not_leave_staged_only_enabled(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "staged_guard", _STAGED_GUARD_VALIDATOR)
    runner = ValidatorRunner(root, mode="in_process")
    target = "events/Algeria.txt"

    result = runner.run("staged_guard", files=[target], post_filter=False)
    validator = runner._modules["validate_staged_guard"].last_instance

    assert result["scoped"] is True
    assert [issue["file"] for issue in result["issues"]] == [target]
    assert validator.staged_only is False


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


def test_unsupported_collector_falls_back_in_process_too(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_legacy_ip", _LEGACY_VALIDATOR)
    runner = ValidatorRunner(root, mode="in_process")

    result = runner.run("scope_legacy_ip", files=["events/Algeria.txt"])

    assert result["ok"] is True
    assert "scoped" not in result
    assert [issue["file"] for issue in result["issues"]] == ["events/Algeria.txt"]


# A callable collector that passes the code-shape check but whose signature
# cannot be introspected.
_TEMPLATE_SOURCE = (
    "def _template(patterns, ignore_staged=False):\n"
    "    staged_only\n"
    "    staged_files\n"
    "    return patterns\n"
)

_UNINTROSPECTABLE_VALIDATOR = (
    _ISSUE
    + f"""
from pathlib import Path

_frame = compile({_TEMPLATE_SOURCE!r}, "<fixture>", "exec")
_borrowed_code = next(
    c for c in _frame.co_consts if hasattr(c, "co_varnames")
)


class _Collector:
    __code__ = _borrowed_code
    __signature__ = 5  # invalid: inspect.signature raises TypeError

    def __init__(self, fn):
        self._fn = fn

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class Validator:
    def __init__(self, mod_path, use_colors=False, staged_only=False, **kwargs):
        self.mod_path = Path(mod_path)
        self.staged_only = staged_only
        self.staged_files = None
        self._issues = []
        self._collect_files = _Collector(self._scan)

    def _scan(self, patterns, ignore_staged=False):
        return sorted((self.mod_path / patterns[0]).glob("*.txt"))

    def run_all_validations(self):
        for path in self._collect_files(["events"]):
            self._issues.append(
                _Issue(
                    severity="warning",
                    category="scan",
                    message="checked",
                    file=path.relative_to(self.mod_path).as_posix(),
                )
            )
"""
)


_WRONG_CODE_SHAPE_VALIDATOR = (
    _ISSUE
    + """
from pathlib import Path


class Validator:
    def __init__(self, mod_path, use_colors=False, staged_only=False, **kwargs):
        self.mod_path = Path(mod_path)
        self._issues = []

    def _collect_files(self, patterns):
        return sorted((self.mod_path / patterns[0]).glob("*.txt"))

    def run_all_validations(self):
        for path in self._collect_files(["events"]):
            self._issues.append(
                _Issue(
                    severity="warning",
                    category="scan",
                    message="checked",
                    file=path.relative_to(self.mod_path).as_posix(),
                )
            )
"""
)


def test_collector_without_staged_signature_falls_back_to_full_scan(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_wrongshape", _WRONG_CODE_SHAPE_VALIDATOR)
    runner = ValidatorRunner(root, mode="in_process")

    result = runner.run("scope_wrongshape", files=["events/Algeria.txt"], post_filter=False)

    assert result["ok"] is True
    assert "scoped" not in result
    assert [issue["file"] for issue in result["issues"]] == [
        "events/Algeria.txt",
        "events/Brazil.txt",
    ]


_LOCKED_COLLECTOR_VALIDATOR = (
    _ISSUE
    + """
from pathlib import Path


class Validator:
    def __init__(self, mod_path, use_colors=False, staged_only=False, **kwargs):
        self.mod_path = Path(mod_path)
        self.staged_only = staged_only
        self.staged_files = None
        self._issues = []

    def __setattr__(self, name, value):
        if name == "_collect_files":
            raise AttributeError("locked")
        super().__setattr__(name, value)

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
        for path in self._collect_files(["events/*.txt"]):
            self._issues.append(
                _Issue(
                    severity="warning",
                    category="scan",
                    message="checked",
                    file=path.relative_to(self.mod_path).as_posix(),
                )
            )
"""
)


def test_locked_collector_falls_back_to_full_scan(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_locked", _LOCKED_COLLECTOR_VALIDATOR)
    runner = ValidatorRunner(root, mode="in_process")

    result = runner.run("scope_locked", files=["events/Algeria.txt"], post_filter=False)

    assert result["ok"] is True
    assert "scoped" not in result
    assert [issue["file"] for issue in result["issues"]] == [
        "events/Algeria.txt",
        "events/Brazil.txt",
    ]


def test_unintrospectable_collector_falls_back_to_full_scan(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_weird", _UNINTROSPECTABLE_VALIDATOR)
    runner = ValidatorRunner(root, mode="in_process")

    result = runner.run("scope_weird", files=["events/Algeria.txt"], post_filter=False)

    assert result["ok"] is True
    assert "scoped" not in result
    assert [issue["file"] for issue in result["issues"]] == [
        "events/Algeria.txt",
        "events/Brazil.txt",
    ]


_MISMATCHED_CALL_VALIDATOR = (
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
        try:
            files = self._collect_files(["events/*.txt"], ignore_staged=True, unexpected=True)
        except TypeError:
            files = []
            self._issues.append(
                _Issue(
                    severity="warning",
                    category="fallback",
                    message="collector rejected mismatched call",
                    file="",
                )
            )
        for path in files:
            self._issues.append(
                _Issue(
                    severity="warning",
                    category="scan",
                    message="checked",
                    file=path.relative_to(self.mod_path).as_posix(),
                )
            )
"""
)


def test_scoped_collect_passes_mismatched_calls_through_unchanged(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_mismatch", _MISMATCHED_CALL_VALIDATOR)
    runner = ValidatorRunner(root, mode="in_process")

    result = runner.run("scope_mismatch", files=["events/Algeria.txt"], post_filter=False)

    assert result["ok"] is True
    assert result["scoped"] is True
    assert [issue["category"] for issue in result["issues"]] == ["fallback"]


def test_scope_rejects_symlink_escaping_the_mod_root(tmp_path):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_symlink", _SCOPED_VALIDATOR)
    outside = tmp_path / "outside.txt"
    outside.write_text("idea_two", encoding="utf-8")
    (root / "events" / "link.txt").symlink_to(outside)
    runner = ValidatorRunner(root, mode="in_process")

    result = runner.run("scope_symlink", files=["events/link.txt"], post_filter=False)

    assert result["ok"] is True
    assert "scoped" not in result


def test_isolated_subprocess_failure_reports_error(tmp_path, monkeypatch):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_boom", _SCOPED_VALIDATOR)
    runner = ValidatorRunner(root)

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=600)

    monkeypatch.setattr(subprocess, "run", _timeout)
    result = runner.run("scope_boom", files=["events/Algeria.txt"])
    assert result["ok"] is False
    assert result["error"] == "Validator timed out after 600s"

    def _spawn_error(*a, **k):
        raise OSError("spawn failed")

    monkeypatch.setattr(subprocess, "run", _spawn_error)
    result = runner.run("scope_boom", files=["events/Algeria.txt"])
    assert result["ok"] is False
    assert "spawn failed" in result["error"]


def _run_shim(tmp_path, monkeypatch, root, files_body, module):
    out = tmp_path / "issues.json"
    argv = ["_shim", "--mod-root", str(root), "--module", module, "--out", str(out)]
    if files_body is not None:
        payload = tmp_path / "files.json"
        payload.write_text(json.dumps(files_body), encoding="utf-8")
        argv += ["--files", str(payload)]
    monkeypatch.setattr(sys, "argv", argv)
    exit_code = _shim.main()
    return exit_code, json.loads(out.read_text(encoding="utf-8"))


def test_shim_main_runs_scoped_validation(tmp_path, monkeypatch):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_shim", _SCOPED_VALIDATOR)

    exit_code, data = _run_shim(
        tmp_path, monkeypatch, root, ["events/Algeria.txt"], "validate_scope_shim"
    )

    assert exit_code == 0
    assert data["ok"] is True
    assert data["scoped"] is True
    assert [issue["file"] for issue in data["issues"]] == ["events/Algeria.txt", ""]


def test_shim_main_rejects_non_list_files_payload(tmp_path, monkeypatch):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_shim_bad", _SCOPED_VALIDATOR)

    exit_code, data = _run_shim(tmp_path, monkeypatch, root, {"a": 1}, "validate_scope_shim_bad")

    assert exit_code == 1
    assert data["ok"] is False
    assert "--files payload must be a list of paths" in data["error"]


def test_shim_main_rejects_non_string_files_entries(tmp_path, monkeypatch):
    root = tmp_path / "Mod"
    _write_fixture(root, "scope_shim_bad2", _SCOPED_VALIDATOR)

    exit_code, data = _run_shim(tmp_path, monkeypatch, root, [1, 2], "validate_scope_shim_bad2")

    assert exit_code == 1
    assert data["ok"] is False
    assert "--files payload must be a list of paths" in data["error"]
