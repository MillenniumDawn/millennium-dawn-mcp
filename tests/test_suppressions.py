from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from md_mcp.analysis.suppressions import suppressed_count
from md_mcp.tools.lint_validators import run_validators_for_lint
from md_mcp.tools.linting_tools import lint_tool
from md_mcp.validators import ValidatorRunner  # pyright: ignore[reportMissingImports]

_ISSUE_CLASS = """
class _Issue:
    def __init__(self, **kw):
        self.kw = kw

    def to_dict(self):
        return dict(self.kw)
"""


_VALIDATOR = (
    _ISSUE_CLASS
    + """
class Validator:
    TITLE = "Synthetic"

    def __init__(self, **kwargs):
        self._issues = []

    def run_all_validations(self):
        self._issues = [
            _Issue(
                severity="warning", category="fixture", message="fixture_known_pattern",
                file="common/x.txt",
            ),
            _Issue(
                severity="error", category="fixture", message="real_problem",
                file="common/x.txt",
            ),
        ]
"""
)


def _write_rule(mod_root: Path, text: str = "fixture_known_pattern") -> None:
    path = mod_root / ".claude" / "docs" / "known-false-positives.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Known false positives\n\n- `{text}` is intentional.\n", encoding="utf-8")


def test_suppressed_count_is_nonnegative_and_best_effort():
    cases: tuple[tuple[dict, int], ...] = (
        ({}, 0),
        ({"suppressed": 3}, 3),
        ({"suppressed": "2"}, 2),
        ({"suppressed": -1}, 0),
        ({"suppressed": "invalid"}, 0),
    )
    for result, expected in cases:
        assert suppressed_count(result) == expected


def test_validator_suppresses_runtime_rule_and_reports_count(fake_mod_root):
    _write_rule(fake_mod_root)
    (fake_mod_root / "common").mkdir(exist_ok=True)
    (fake_mod_root / "common" / "x.txt").write_text("x = 1\n", encoding="utf-8")
    validator = fake_mod_root / "tools" / "validation" / "validate_synthetic.py"
    validator.write_text(_VALIDATOR, encoding="utf-8")

    result = ValidatorRunner(fake_mod_root).run("synthetic")

    assert result["suppressed"] == 1
    assert result["suppression_source"] == ".claude/docs/known-false-positives.md"
    assert [issue["message"] for issue in result["issues"]] == ["real_problem"]
    assert result["counts"] == {"error": 1, "warning": 0, "info": 0}


def test_lint_suppresses_fake_runner_issues_and_preserves_scope_counts(tmp_path):
    class Runner:
        def run(self, name, *, staged_only=False):
            return {
                "ok": True,
                "issues": [
                    {
                        "file": "common/x.txt",
                        "message": "fixture_known_pattern",
                        "severity": "warning",
                    }
                ],
            }

    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "x.txt").write_text("x = 1\n", encoding="utf-8")
    _write_rule(tmp_path)

    entries, issues = run_validators_for_lint(
        cast(Any, Runner()),
        ["style"],
        staged_only=False,
        relevant_set={"common/x.txt"},
        mod_root=tmp_path,
    )

    assert issues == []
    assert entries == [
        {
            "name": "validator:style",
            "ok": True,
            "total": 0,
            "suppressed": 1,
            "suppression_source": ".claude/docs/known-false-positives.md",
            "total_mod_wide": 1,
        }
    ]


def test_lint_script_suppression_is_reported(tmp_path):
    _write_rule(tmp_path)
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "x.txt").write_text("x = 1\n", encoding="utf-8")
    script = tmp_path / "tools" / "linting" / "check_common_mistakes.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        'import sys\nprint("common/x.txt:1: fixture_known_pattern")\nsys.exit(1)\n',
        encoding="utf-8",
    )

    result = lint_tool(
        tmp_path,
        files=["common/x.txt"],
        checks=["common_mistakes"],
        validators=[],
    )

    assert result["suppressed"] == 1
    assert result["counts"] == {"error": 0, "warning": 0, "info": 0}
    assert result["issues"] == []


def test_missing_runtime_rule_is_a_graceful_noop(tmp_path):
    class Runner:
        def run(self, name, *, staged_only=False):
            return {
                "ok": True,
                "issues": [
                    {
                        "message": "fixture_known_pattern",
                        "severity": "warning",
                        "file": "common/x.txt",
                    }
                ],
            }

    entries, issues = run_validators_for_lint(
        cast(Any, Runner()),
        ["style"],
        staged_only=False,
        relevant_set=None,
        mod_root=tmp_path,
    )

    assert entries == [{"name": "validator:style", "ok": True, "total": 1}]
    assert len(issues) == 1
    assert issues[0]["message"] == "fixture_known_pattern"
