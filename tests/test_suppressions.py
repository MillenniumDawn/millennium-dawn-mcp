from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from md_mcp.analysis.suppressions import suppress_issues, suppressed_count
from md_mcp.config import Settings
from md_mcp.tools.lint_validators import run_validators_for_lint
from md_mcp.tools.linting_tools import lint_tool
from md_mcp.tools.validation_tools import validate_tool
from md_mcp.validators import (  # pyright: ignore[reportMissingImports]
    ValidatorInfo,
    ValidatorRunner,
)

_SOURCE = ".claude/docs/known-false-positives.md"

_UPSTREAM_DOC = Path(__file__).parent / "fixtures" / "known_false_positives.md"

_FOCUS_MESSAGE = "Missing icon sprite 'GFX_vanilla_only' for focus 'TST_focus'"
_DECISION_MESSAGE = (
    "TST_decision: icon = vanilla_only -> no sprite vanilla_only / GFX_decision_vanilla_only / "
    "GFX_vanilla_only defined in interface/*.gfx (create the sprite or pick an existing icon)"
)

_ISSUE_CLASS = """
class _Issue:
    def __init__(self, **kw):
        self.kw = kw

    def to_dict(self):
        return dict(self.kw)
"""

_VALIDATOR = (
    _ISSUE_CLASS
    + f"""
class Validator:
    TITLE = "Synthetic"

    def __init__(self, **kwargs):
        self._issues = []

    def run_all_validations(self):
        self._issues = [
            _Issue(
                severity="warning", category="missing-focus-icon",
                message={_FOCUS_MESSAGE!r}, file="common/x.txt",
            ),
            _Issue(
                severity="error", category="fixture", message="real_problem",
                file="common/x.txt",
            ),
        ]
"""
)


def _write_manifest(mod_root: Path, *names: str) -> None:
    path = mod_root / "tools" / "validation" / "vanilla_sprites.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# header\n" + "".join(f"{name} 32x32\n" for name in names), encoding="utf-8")


def _write_upstream_doc(mod_root: Path) -> None:
    path = mod_root / ".claude" / "docs" / "known-false-positives.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_UPSTREAM_DOC.read_text(encoding="utf-8"), encoding="utf-8")


def _issue(category: str, message: str) -> dict:
    return {"severity": "warning", "category": category, "message": message, "file": "x.txt"}


def test_suppressed_count_is_nonnegative_and_best_effort():
    cases: tuple[tuple[dict, int], ...] = (
        ({}, 0),
        ({"suppressed": 3}, 3),
        ({"suppressed_mod_wide": 3}, 3),
        ({"suppressed": "2"}, 2),
        ({"suppressed_mod_wide": "2"}, 2),
        ({"suppressed": -1}, 0),
        ({"suppressed_mod_wide": -1}, 0),
        ({"suppressed": "invalid"}, 0),
    )
    for result, expected in cases:
        assert suppressed_count(result) == expected


@pytest.mark.parametrize(
    ("category", "message"),
    [
        ("missing-focus-icon", _FOCUS_MESSAGE),
        ("missing-decision-icon", _DECISION_MESSAGE),
    ],
)
def test_icon_finding_is_suppressed_when_manifest_lists_the_sprite(tmp_path, category, message):
    _write_manifest(tmp_path, "GFX_vanilla_only", "GFX_decision_vanilla_only")
    keep = _issue("missing-focus-icon", "Missing icon sprite 'GFX_mod_typo' for focus 'X'")

    kept, suppressed = suppress_issues([_issue(category, message), keep], tmp_path)

    assert suppressed == 1
    assert kept == [keep]


def test_decision_finding_matches_any_candidate_in_manifest(tmp_path):
    _write_manifest(tmp_path, "GFX_vanilla_only")

    kept, suppressed = suppress_issues(
        [_issue("missing-decision-icon", _DECISION_MESSAGE)], tmp_path
    )

    assert (kept, suppressed) == ([], 1)


@pytest.mark.parametrize(
    ("category", "message"),
    [
        ("missing-focus-icon", _FOCUS_MESSAGE),
        ("missing-decision-icon", _DECISION_MESSAGE),
    ],
)
def test_icon_finding_stays_when_sprite_is_not_in_manifest(tmp_path, category, message):
    _write_manifest(tmp_path, "GFX_other")
    issue = _issue(category, message)

    assert suppress_issues([issue], tmp_path) == ([issue], 0)


def test_no_manifest_means_no_suppression(tmp_path):
    issue = _issue("missing-focus-icon", _FOCUS_MESSAGE)

    assert suppress_issues([issue], tmp_path) == ([issue], 0)


@pytest.mark.parametrize("category", ["missing-idea-icon", "missing-event-picture", "fixture", ""])
def test_other_categories_are_never_suppressed(tmp_path, category):
    _write_manifest(tmp_path, "GFX_vanilla_only", "GFX_decision_vanilla_only")
    issue = _issue(category, _FOCUS_MESSAGE)

    assert suppress_issues([issue], tmp_path) == ([issue], 0)


def test_unrelated_messages_with_upstream_prose_words_stay_visible(tmp_path):
    _write_upstream_doc(tmp_path)
    _write_manifest(tmp_path, "GFX_vanilla_only")
    issues = [
        _issue("trigger", "invalid trigger num_of_civilian_factories in total civilian block"),
        _issue("style", "hidden_trigger = { } directly inside custom_trigger_tooltip is redundant"),
        _issue("sprite", "missing sprite GFX_nowhere referenced by an idea"),
        _issue("style", "treasury_change after one_random_building_effect double-charges"),
        _issue("missing-focus-icon", "sprite missing but no quoted name"),
        _issue("missing-decision-icon", "decision icon missing sprite"),
    ]

    assert suppress_issues(issues, tmp_path) == (issues, 0)


def test_validator_suppresses_manifest_backed_finding_and_reports_count(fake_mod_root):
    _write_manifest(fake_mod_root, "GFX_vanilla_only")
    (fake_mod_root / "common").mkdir(exist_ok=True)
    (fake_mod_root / "common" / "x.txt").write_text("x = 1\n", encoding="utf-8")
    validator = fake_mod_root / "tools" / "validation" / "validate_synthetic.py"
    validator.write_text(_VALIDATOR, encoding="utf-8")

    result = ValidatorRunner(fake_mod_root).run("synthetic")

    assert result["suppressed"] == 1
    assert result["suppression_source"] == _SOURCE
    assert [issue["message"] for issue in result["issues"]] == ["real_problem"]
    assert result["counts"] == {"error": 1, "warning": 0, "info": 0}


def test_lint_reports_runner_suppressed_count_without_doing_its_own_pass(tmp_path):
    """The runner already drops suppressed findings; lint only reads the count.

    A FakeRunner that returns both an empty issue list (suppressed issue was
    removed) and a `suppressed` count exercises the bridge: lint must not call
    `suppress_issues` again, and `total_mod_wide` must include the suppressed
    count so callers can see the mod-wide census.
    """

    class Runner:
        def run(self, name, *, staged_only=False, files=None, post_filter=True):
            return {
                "ok": True,
                "issues": [],
                "suppressed": 1,
                "suppression_source": _SOURCE,
            }

    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "x.txt").write_text("x = 1\n", encoding="utf-8")
    _write_manifest(tmp_path, "GFX_vanilla_only")

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
            "suppressed_mod_wide": 1,
            "suppression_source": _SOURCE,
            "total_mod_wide": 1,
        }
    ]


def test_lint_does_not_suppress_when_runner_returned_no_count(tmp_path):
    """Regression for the duplicate-pass fix: if the runner reported zero
    suppressed findings, lint must not invoke `suppress_issues` itself and
    invent a count.
    """

    class Runner:
        def run(self, name, *, staged_only=False, files=None, post_filter=True):
            return {
                "ok": True,
                "issues": [
                    {
                        "file": "common/x.txt",
                        "message": _FOCUS_MESSAGE,
                        "category": "missing-focus-icon",
                        "severity": "warning",
                    }
                ],
            }

    _write_manifest(tmp_path, "GFX_vanilla_only")
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "x.txt").write_text("x = 1\n", encoding="utf-8")

    # Sanity: manifest is in place; an opportunistic lint pass would match.
    from md_mcp.analysis.suppressions import suppress_issues

    baseline_kept, baseline_suppressed = suppress_issues([], tmp_path)
    assert baseline_kept == []
    assert baseline_suppressed == 0

    entries, issues = run_validators_for_lint(
        cast(Any, Runner()),
        ["style"],
        staged_only=False,
        relevant_set={"common/x.txt"},
        mod_root=tmp_path,
    )

    assert "suppressed_mod_wide" not in entries[0]
    assert "suppression_source" not in entries[0]
    assert entries[0]["total"] == 1
    assert entries[0]["total_mod_wide"] == 1
    assert len(issues) == 1


def test_lint_tool_reports_validator_suppressions_in_summary(tmp_path):
    """lint_tool rolls up `suppressed_mod_wide` from each validator entry.

    The FakeRunner simulates a real runner that already dropped the manifest-
    backed issue: its issue list is empty, the suppressed count is reported
    on the result. lint_tool must fold that count into the top-level
    `suppressed_mod_wide` field without calling `suppress_issues` again.
    """

    class Runner(ValidatorRunner):
        def __init__(self) -> None:
            super().__init__(tmp_path)

        def list(self):
            return [
                ValidatorInfo(
                    name="style", module_name="validate_style", title="style", path=tmp_path
                )
            ]

        def run(self, name, *, staged_only=False, files=None, post_filter=True, args=None):
            return {
                "ok": True,
                "issues": [],
                "suppressed": 1,
                "suppression_source": _SOURCE,
            }

    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "x.txt").write_text("x = 1\n", encoding="utf-8")
    _write_manifest(tmp_path, "GFX_vanilla_only")

    result = lint_tool(
        tmp_path,
        files=["common/x.txt"],
        checks=[],
        validators=["style"],
        validator_runner=Runner(),
    )

    assert result["suppressed_mod_wide"] == 1
    assert "suppressed" not in result
    assert result["suppression_source"] == _SOURCE
    assert result["issues"] == []


def test_lint_script_output_matching_upstream_prose_stays_visible(tmp_path):
    _write_upstream_doc(tmp_path)
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "x.txt").write_text("x = 1\n", encoding="utf-8")
    script = tmp_path / "tools" / "linting" / "check_common_mistakes.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\n"
        'print("common/x.txt:1: hidden_trigger = { } directly inside '
        'custom_trigger_tooltip is redundant")\n'
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    result = lint_tool(
        tmp_path,
        files=["common/x.txt"],
        checks=["common_mistakes"],
        validators=[],
    )

    assert "suppressed_mod_wide" not in result
    assert len(result["issues"]) == 1


def test_missing_manifest_is_a_graceful_noop(tmp_path):
    class Runner:
        def run(self, name, *, staged_only=False, files=None, post_filter=True):
            return {
                "ok": True,
                "issues": [
                    {
                        "message": _FOCUS_MESSAGE,
                        "category": "missing-focus-icon",
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


def test_validate_all_reports_suppressed_findings_in_summary(fake_mod_root):
    _write_manifest(fake_mod_root, "GFX_vanilla_only")
    (fake_mod_root / "common").mkdir(exist_ok=True)
    (fake_mod_root / "common" / "x.txt").write_text("x = 1\n", encoding="utf-8")
    validator = fake_mod_root / "tools" / "validation" / "validate_synthetic.py"
    validator.write_text(_VALIDATOR, encoding="utf-8")
    settings = Settings(
        mod_root=fake_mod_root,
        vanilla_path=None,
        cache_dir=fake_mod_root / ".md-mcp-cache",
        validator_mode="in_process",
    )

    result = validate_tool(settings, ValidatorRunner(fake_mod_root))

    assert result["suppressed"] == 1
    assert result["suppression_source"] == _SOURCE
    entry = result["validators"][0]
    assert entry["suppressed"] == 1
    assert entry["suppression_source"] == _SOURCE
    assert result["counts"] == {"error": 1, "warning": 0, "info": 0}
