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
