"""Tests for the validate MCP tool's aggregation logic.

The tool runs every fast validator when no name is given; the run-all summary
must not report success while individual validators failed.
"""

from __future__ import annotations

from pathlib import Path

from md_mcp.config import Settings
from md_mcp.tools.validation_tools import validate_tool
from md_mcp.validators import ValidatorRunner

_ISSUE_CLASS = """
class _Issue:
    def __init__(self, **kw):
        self.kw = kw

    def to_dict(self):
        return dict(self.kw)
"""

_GOOD = (
    _ISSUE_CLASS
    + """
class Validator:
    TITLE = "Good"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self._issues = []

    def run_all_validations(self):
        self._issues = [
            _Issue(severity="warning", category="fake", message="m1", file="events/a.txt", line=3),
            _Issue(severity="error", category="fake", message="m2"),
        ]
"""
)

_WARNING_ONLY = (
    _ISSUE_CLASS
    + """
class Validator:
    TITLE = "WarnOnly"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self._issues = []

    def run_all_validations(self):
        self._issues = [
            _Issue(severity="warning", category="fake", message="w1", file="events/a.txt", line=1),
            _Issue(severity="warning", category="fake", message="w2", file="events/a.txt", line=2),
        ]
"""
)

_BROKEN = "this is not valid python (\n"


def _plant(mod_root: Path, name: str, source: str) -> None:
    (mod_root / "tools" / "validation" / f"validate_{name}.py").write_text(source, encoding="utf-8")


def _settings(mod_root: Path) -> Settings:
    return Settings(mod_root=mod_root, vanilla_path=None, cache_dir=mod_root / ".md-mcp-cache")


def test_validate_all_ok_when_every_validator_ok(fake_mod_root):
    _plant(fake_mod_root, "good", _GOOD)
    result = validate_tool(_settings(fake_mod_root), ValidatorRunner(fake_mod_root))
    assert result["ok"] is True
    assert result["validators"] == [
        {
            "name": "good",
            "title": "Good",
            "ok": True,
            "counts": {"error": 1, "warning": 1, "info": 0},
            "error": None,
        }
    ]
    assert result["counts"] == {"error": 1, "warning": 1, "info": 0}


def test_validate_all_top_level_failure_when_any_validator_fails(fake_mod_root):
    # Regression: the run-all summary used to hardcode ok=True, burying a
    # broken validator in the per-validator list.
    _plant(fake_mod_root, "good", _GOOD)
    _plant(fake_mod_root, "broken", _BROKEN)
    result = validate_tool(_settings(fake_mod_root), ValidatorRunner(fake_mod_root))
    assert result["ok"] is False
    good = next(v for v in result["validators"] if v["name"] == "good")
    broken = next(v for v in result["validators"] if v["name"] == "broken")
    assert good["ok"] is True
    assert broken["ok"] is False
    assert "SyntaxError" in broken["error"]
    # Failed runs contribute no issues or counts to the summary.
    assert result["counts"] == {"error": 1, "warning": 1, "info": 0}


def test_validate_single_validator_propagates_failure(fake_mod_root):
    _plant(fake_mod_root, "broken", _BROKEN)
    result = validate_tool(
        _settings(fake_mod_root), ValidatorRunner(fake_mod_root), validator="broken"
    )
    assert result["ok"] is False
    assert "SyntaxError" in result["error"]


def test_validate_single_validator_strict_folds_warnings_into_errors(fake_mod_root):
    _plant(fake_mod_root, "warnonly", _WARNING_ONLY)
    result = validate_tool(
        _settings(fake_mod_root),
        ValidatorRunner(fake_mod_root),
        validator="warnonly",
        strict=True,
    )
    assert result["ok"] is True
    assert result["counts"] == {"error": 2, "warning": 0, "info": 0}


def test_validate_single_validator_non_strict_counts_unchanged(fake_mod_root):
    _plant(fake_mod_root, "warnonly", _WARNING_ONLY)
    result = validate_tool(
        _settings(fake_mod_root), ValidatorRunner(fake_mod_root), validator="warnonly"
    )
    assert result["ok"] is True
    assert result["counts"] == {"error": 0, "warning": 2, "info": 0}


def test_validate_all_strict_still_folds_aggregate_counts(fake_mod_root):
    _plant(fake_mod_root, "warnonly", _WARNING_ONLY)
    result = validate_tool(_settings(fake_mod_root), ValidatorRunner(fake_mod_root), strict=True)
    assert result["ok"] is True
    assert result["counts"] == {"error": 2, "warning": 0, "info": 0}


def test_validate_all_strict_folds_each_validator_breakdown(fake_mod_root):
    """The per-validator counts must sum to the strict total (#54).

    They were captured before the fold, so `overall` was strict while the
    breakdown stayed raw and a caller reconciling the two saw a warning count
    that appeared in one place and not the other.
    """
    _plant(fake_mod_root, "warnonly", _WARNING_ONLY)
    _plant(fake_mod_root, "good", _GOOD)

    result = validate_tool(_settings(fake_mod_root), ValidatorRunner(fake_mod_root), strict=True)

    assert result["counts"] == {"error": 4, "warning": 0, "info": 0}

    by_name = {v["name"]: v["counts"] for v in result["validators"]}
    assert by_name["warnonly"] == {"error": 2, "warning": 0, "info": 0}
    assert by_name["good"] == {"error": 2, "warning": 0, "info": 0}

    # The property the issue names, asserted directly rather than implied by
    # the two equalities above.
    for key in ("error", "warning", "info"):
        assert sum(c.get(key, 0) for c in by_name.values()) == result["counts"][key]


def test_validate_all_non_strict_breakdown_is_unchanged(fake_mod_root):
    """Control. A fold applied unconditionally would pass the test above."""
    _plant(fake_mod_root, "warnonly", _WARNING_ONLY)
    _plant(fake_mod_root, "good", _GOOD)

    result = validate_tool(_settings(fake_mod_root), ValidatorRunner(fake_mod_root))

    assert result["counts"] == {"error": 1, "warning": 3, "info": 0}
    by_name = {v["name"]: v["counts"] for v in result["validators"]}
    assert by_name["warnonly"] == {"error": 0, "warning": 2, "info": 0}
    assert by_name["good"] == {"error": 1, "warning": 1, "info": 0}


def test_validate_all_strict_leaves_a_failed_validator_without_counts(fake_mod_root):
    """A validator that could not run reported no counts, and strict must not
    invent an {"error": 0, "warning": 0} for it."""
    _plant(fake_mod_root, "warnonly", _WARNING_ONLY)
    _plant(fake_mod_root, "broken", _BROKEN)

    result = validate_tool(_settings(fake_mod_root), ValidatorRunner(fake_mod_root), strict=True)

    broken = next(v for v in result["validators"] if v["name"] == "broken")
    assert broken["ok"] is False
    assert broken["counts"] == {}
