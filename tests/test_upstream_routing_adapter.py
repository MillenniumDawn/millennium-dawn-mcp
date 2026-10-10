"""Unit tests for the optional upstream CI routing adapter."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import md_mcp.tools.lint_validators as lint_validators
from md_mcp.tools.lint_validators import (
    _load_upstream_module,
    _upstream_args,
    _upstream_routing,
    _upstream_validators_for_paths,
    run_validators_for_lint,
    select_validators,
)
from md_mcp.validators import ValidatorRunner

_BATCHES = """
class Spec:
    def __init__(self, script, groups=(), args=()):
        self.script = script
        self.groups = groups
        self.args = args


ALL_SPECS = [
    Spec("tools/validation/validate_alpha.py", ("core",), ("--alpha-mode",)),
    Spec("tools/validation/validate_ignored.py", ("core",)),
    Spec("tools/validation/validate_tool.py", ("tooling",), ("--tool-mode",)),
]
IMPACT_ONLY_SPECS = [
    Spec("tools/validation/validate_file_paths.py"),
    Spec("tools/validation/validate_style.py", args=("--style-mode",)),
    Spec("tools/validation/validate_mod_descriptors.py"),
]
_IMPACT_EXCLUDED_SCRIPTS = ("tools/validation/validate_ignored.py",)


def select_for_changed_files(paths):
    if "tools/shared_utils.py" in paths:
        return [ALL_SPECS[2]], [IMPACT_ONLY_SPECS[1], IMPACT_ONLY_SPECS[2]]
    return [], []
"""

_GROUPS = """
def classify(paths):
    return {
        "core": any(path.startswith("common/") for path in paths),
        "file-paths": any(path.endswith(".mod") for path in paths),
        "style": any(path.endswith(".txt") for path in paths),
    }
"""


def _install_routing(root: Path, *, include_groups: bool = True) -> Path:
    validation = root / "tools" / "validation"
    validation.mkdir(parents=True, exist_ok=True)
    (validation / "validator_batches.py").write_text(_BATCHES, encoding="utf-8")
    if include_groups:
        (validation / "change_groups.py").write_text(_GROUPS, encoding="utf-8")
    return root


def test_upstream_module_loader_handles_missing_specs_and_import_errors(tmp_path, monkeypatch):
    source = tmp_path / "helper.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(lint_validators.importlib.util, "spec_from_file_location", lambda *_: None)
    assert _load_upstream_module(tmp_path, "helper.py", "md_test_no_spec") is None

    monkeypatch.setattr(
        lint_validators.importlib.util,
        "spec_from_file_location",
        lambda *_: SimpleNamespace(loader=None),
    )
    assert _load_upstream_module(tmp_path, "helper.py", "md_test_no_loader") is None
    assert _load_upstream_module(tmp_path, "missing.py", "md_test_missing") is None

    monkeypatch.undo()
    source.write_text("raise RuntimeError('broken helper')\n", encoding="utf-8")
    for name, previous in (
        ("md_test_broken_without_prior", None),
        ("md_test_broken_with_prior", ModuleType("md_test_broken_with_prior")),
    ):
        if previous is not None:
            sys.modules[name] = previous
        with pytest.raises(RuntimeError, match="broken helper"):
            _load_upstream_module(tmp_path, "helper.py", name)
        if previous is None:
            assert name not in sys.modules
        else:
            assert sys.modules[name] is previous
            del sys.modules[name]


def test_upstream_routing_falls_back_for_partial_checkouts(tmp_path):
    assert _upstream_routing(str(tmp_path)) is None
    _install_routing(tmp_path, include_groups=False)
    assert _upstream_routing(str(tmp_path)) is None
    assert _upstream_args(tmp_path) == {}
    assert select_validators(
        ["common/national_focus/USA.txt"], {"focus_tree"}, mod_root=tmp_path
    ) == ["focus_tree"]


def test_content_routes_use_upstream_groups_and_impact_only_specs(tmp_path):
    _install_routing(tmp_path)
    available = {"alpha", "ignored", "style", "file_paths", "mod_descriptors"}

    names = _upstream_validators_for_paths(["common/ideas/a.txt"], tmp_path)
    assert names == {"alpha", "style"}
    args = _upstream_args(tmp_path)
    assert args["alpha"] == ("--alpha-mode",)
    assert args["style"] == ("--style-mode",)
    assert select_validators(["common/ideas/a.txt"], available, mod_root=tmp_path) == [
        "alpha",
        "style",
    ]

    names = _upstream_validators_for_paths(["descriptor.mod"], tmp_path)
    assert names == {"file_paths", "mod_descriptors"}
    assert select_validators(["descriptor.mod"], available, mod_root=tmp_path) == [
        "file_paths",
        "mod_descriptors",
    ]


def test_tool_routes_and_validator_args_use_upstream_specs(tmp_path):
    _install_routing(tmp_path)
    available = {"tool", "style", "mod_descriptors"}
    assert _upstream_validators_for_paths([r"tools\shared_utils.py"], tmp_path) == available
    assert _upstream_args(tmp_path)["tool"] == ("--tool-mode",)
    assert select_validators([r"tools\shared_utils.py"], available, mod_root=tmp_path) == [
        "mod_descriptors",
        "style",
        "tool",
    ]
    assert _upstream_args(tmp_path) == {
        "alpha": ("--alpha-mode",),
        "tool": ("--tool-mode",),
        "style": ("--style-mode",),
    }

    class RecordingRunner(ValidatorRunner):
        def __init__(self):
            super().__init__(Path("/unused"))
            self.calls = []

        def run(self, name, *, staged_only=False, files=None, post_filter=True, args=None):
            kwargs = {"staged_only": staged_only}
            if files is not None:
                kwargs["files"] = files
            if not post_filter:
                kwargs["post_filter"] = post_filter
            if args is not None:
                kwargs["args"] = args
            self.calls.append((name, kwargs))
            return {"ok": True, "issues": []}

    runner = RecordingRunner()
    entries, issues = run_validators_for_lint(
        runner,
        ["alpha"],
        staged_only=False,
        relevant_set=None,
        mod_root=tmp_path,
    )
    assert runner.calls == [("alpha", {"staged_only": False, "args": ("--alpha-mode",)})]
    assert entries[0]["ok"] is True
    assert issues == []


def test_upstream_args_without_mod_root_and_run_all_exclusions(tmp_path):
    assert _upstream_args(None) == {}
    _install_routing(tmp_path)
    available = {"alpha", "ignored", "tool", "style", "file_paths", "mod_descriptors"}
    assert select_validators(None, available, mod_root=tmp_path) == [
        "alpha",
        "file_paths",
        "mod_descriptors",
        "style",
        "tool",
    ]
