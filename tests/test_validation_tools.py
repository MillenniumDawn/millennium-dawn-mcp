"""Tests for the validate MCP tool's aggregation logic.

The tool runs every fast validator when no name is given; the run-all summary
must not report success while individual validators failed.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, cast

import pytest

from md_mcp.config import Settings
from md_mcp.tools import validation_tools
from md_mcp.tools.validation_tools import validate_list_tool, validate_tool
from md_mcp.util.response import BUDGET_BYTES
from md_mcp.validators import ValidatorRunner

_validate_tool_with_delta = cast(Callable[..., dict], validate_tool)

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


def test_validate_list_paginates_and_normalizes_bounds(fake_mod_root):
    for name in ("first", "second", "third"):
        _plant(fake_mod_root, name, _GOOD)

    result = validate_list_tool(_settings(fake_mod_root), limit="1.9", offset=1.9)

    assert result["total"] == 3
    assert result["returned"] == 1
    assert result["truncated"] is True
    assert result["validators"] == [
        {"name": "second", "title": "Good", "title_source": "scraped", "module": "validate_second"}
    ]


def test_validate_list_budget_guard_drops_oversized_validator_page(fake_mod_root, monkeypatch):
    class _Info:
        def __init__(self, name):
            self.name = name
            self.title = "x" * 100
            self.title_source = "derived"
            self.module_name = f"validate_{name}"

    infos = [_Info(f"validator_{i}") for i in range(2_000)]
    monkeypatch.setattr(validation_tools, "available_validators", lambda _: infos)

    result = validate_list_tool(_settings(fake_mod_root), limit=2_000)

    assert result["ok"] is True
    assert result["total"] == 2_000
    assert result["returned"] == 2_000
    assert result["truncated"] is False
    assert result["size_truncated"] is True
    assert "validators" not in result
    assert len(json.dumps(result).encode("utf-8")) <= BUDGET_BYTES


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


def _fake_report_lib():
    class Issue:
        def __init__(self, **values):
            self.severity = values.get("severity", "error")
            self.category = values.get("category", "")
            self.message = values.get("message", "")
            self.file = values.get("file", "")
            self.line = values.get("line", 0)
            self.validator = values.get("validator", "")
            self.detected_by = values.get("detected_by", [])

        @classmethod
        def from_dict(cls, values, validator=""):
            return cls(
                severity=values.get("severity", "error"),
                category=values.get("category", ""),
                message=values.get("message", ""),
                file=values.get("file", ""),
                line=int(values.get("line", 0) or 0),
                validator=values.get("validator", validator),
                detected_by=list(values.get("detected_by", [])),
            )

        def to_dict(self):
            return {
                "severity": self.severity,
                "category": self.category,
                "message": self.message,
                "file": self.file,
                "line": self.line,
                "validator": self.validator,
                "detected_by": self.detected_by,
            }

    class Baseline:
        def __init__(self, meta, keys):
            self.meta = meta
            self.keys = keys

    def issue_key(issue):
        if not issue.category or not issue.file or issue.line <= 0 or not issue.message:
            return None
        return (issue.severity, issue.category, issue.file, issue.line, issue.message)

    def dedupe(issues):
        return list(
            {
                (issue.category, issue.file, issue.line, issue.message): issue for issue in issues
            }.values()
        )

    def classify(issues, baseline):
        new_issues = []
        for issue in issues:
            if issue_key(issue) is not None and issue_key(issue) not in baseline.keys:
                new_issues.append(issue)
        return SimpleNamespace(new_issues=new_issues)

    def load_issues(directory):
        issues: list[Issue] = []
        for path in Path(directory).glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                issues.extend(Issue.from_dict(item, validator=path.stem) for item in data)
        return dedupe(issues)

    return SimpleNamespace(
        Issue=Issue,
        Baseline=Baseline,
        issue_key=issue_key,
        dedupe=dedupe,
        classify=classify,
        load_issues=load_issues,
    )


def _delta_settings(mod_root):
    (mod_root / "events").mkdir(exist_ok=True)
    (mod_root / "events" / "a.txt").write_text("synthetic", encoding="utf-8")
    return _settings(mod_root)


def _delta_helper():
    return importlib.import_module("md_mcp.analysis.issue_delta").new_issue_dicts


def _patch_fake_report_lib(monkeypatch):
    fake_report = _fake_report_lib()
    helper_globals = _delta_helper().__globals__
    monkeypatch.setitem(helper_globals, "_report_lib", lambda _: fake_report)
    return fake_report


def _patch_fake_baseline(monkeypatch, baseline_issues):
    _patch_fake_report_lib(monkeypatch)
    helper_globals = _delta_helper().__globals__
    monkeypatch.setitem(
        helper_globals,
        "_load_baseline_issues",
        lambda _settings, _baseline, _lib: baseline_issues,
    )


class _FakeRunner:
    def __init__(self, issues):
        self.issues = issues

    def run(self, validator, **kwargs):
        counts = {"error": 0, "warning": 0, "info": 0}
        for issue in self.issues:
            severity = issue.get("severity", "info")
            counts[severity] += 1
        return {"ok": True, "validator": validator, "counts": counts, "issues": self.issues}


def test_validate_delta_returns_only_synthetic_new_issue(fake_mod_root, monkeypatch):
    settings = _delta_settings(fake_mod_root)
    fake_report = _fake_report_lib()
    baseline = [
        fake_report.Issue.from_dict(
            {
                "severity": "warning",
                "category": "synthetic",
                "message": "existing",
                "file": "events/a.txt",
                "line": 3,
            }
        )
    ]
    _patch_fake_baseline(monkeypatch, baseline)
    runner = _FakeRunner(
        [
            {
                "severity": "warning",
                "category": "synthetic",
                "message": "existing",
                "file": "a.txt",
                "line": 3,
            },
            {
                "severity": "error",
                "category": "synthetic",
                "message": "new finding",
                "file": "a.txt",
                "line": 4,
            },
        ]
    )

    result = _validate_tool_with_delta(
        settings, runner, validator="synthetic", delta=True, baseline="fake"
    )

    assert result["ok"] is True
    assert result["counts"] == {"error": 1, "warning": 0, "info": 0}
    assert result["issues_total_after_filter"] == 1
    assert result["issues"][0]["message"] == "new finding"
    assert result["issues"][0]["file"] == "events/a.txt"


def test_validate_delta_caps_only_new_issues(fake_mod_root, monkeypatch):
    settings = _delta_settings(fake_mod_root)
    _patch_fake_baseline(monkeypatch, [])
    runner = _FakeRunner(
        [
            {
                "severity": "error",
                "category": "synthetic",
                "message": "first",
                "file": "events/a.txt",
                "line": 3,
            },
            {
                "severity": "warning",
                "category": "synthetic",
                "message": "second",
                "file": "events/a.txt",
                "line": 4,
            },
        ]
    )

    result = _validate_tool_with_delta(
        settings, runner, validator="synthetic", delta=True, baseline="fake", limit=1
    )

    assert result["counts"] == {"error": 1, "warning": 1, "info": 0}
    assert result["issues_total_after_filter"] == 2
    assert result["truncated"] is True
    assert len(result["issues"]) == 1


def test_validate_delta_does_not_invent_counts_for_failed_validator(fake_mod_root, monkeypatch):
    _plant(fake_mod_root, "good", _GOOD)
    _plant(fake_mod_root, "broken", _BROKEN)
    _patch_fake_baseline(monkeypatch, [])

    class Runner:
        def run(self, validator, **kwargs):
            if validator == "broken":
                return {"ok": False, "error": "failed"}
            return {"ok": True, "counts": {"error": 0, "warning": 0, "info": 0}, "issues": []}

    result = _validate_tool_with_delta(_settings(fake_mod_root), Runner(), delta=True, strict=True)

    broken = next(entry for entry in result["validators"] if entry["name"] == "broken")
    assert result["ok"] is False
    assert broken["counts"] == {}


def test_validate_delta_missing_default_snapshot_returns_actionable_error(
    fake_mod_root, monkeypatch
):
    settings = _delta_settings(fake_mod_root)
    _patch_fake_report_lib(monkeypatch)

    result = _validate_tool_with_delta(settings, _FakeRunner([]), validator="synthetic", delta=True)

    expected_path = settings.cache_dir / "validator-baselines" / "main.json"
    assert result["ok"] is False
    assert str(expected_path) in result["error"]
    assert "snapshot file or directory" in result["error"]


def test_validate_delta_returns_unkeyable_issue_as_new(fake_mod_root, monkeypatch):
    settings = _delta_settings(fake_mod_root)
    _patch_fake_baseline(monkeypatch, [])
    issue = {
        "severity": "warning",
        "category": "synthetic",
        "message": "unlocated finding",
        "file": "",
    }

    result = _validate_tool_with_delta(
        settings, _FakeRunner([issue]), validator="synthetic", delta=True, baseline="fake"
    )

    assert result["ok"] is True
    assert len(result["issues"]) == 1
    assert result["issues"][0]["file"] == ""
    assert result["issues"][0]["line"] == 0


def test_validate_delta_bare_main_is_a_ref_not_a_cwd_file(fake_mod_root, monkeypatch, tmp_path):
    settings = _delta_settings(fake_mod_root)
    _patch_fake_report_lib(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main").write_text("not a snapshot", encoding="utf-8")

    result = _validate_tool_with_delta(
        settings, _FakeRunner([]), validator="synthetic", delta=True, baseline="main"
    )

    expected_path = settings.cache_dir / "validator-baselines" / "main.json"
    assert result["ok"] is False
    assert str(expected_path) in result["error"]
    assert "Could not read baseline snapshot" not in result["error"]


def test_validate_delta_clean_run_returns_empty_issues(fake_mod_root, monkeypatch):
    settings = _delta_settings(fake_mod_root)
    fake_report = _fake_report_lib()
    issue = {
        "severity": "error",
        "category": "synthetic",
        "message": "already known",
        "file": "events/a.txt",
        "line": 3,
    }
    _patch_fake_baseline(monkeypatch, [fake_report.Issue.from_dict(issue)])

    result = _validate_tool_with_delta(
        settings, _FakeRunner([issue]), validator="synthetic", delta=True, baseline="fake"
    )

    assert result["ok"] is True
    assert result["counts"] == {"error": 0, "warning": 0, "info": 0}
    assert result["issues_total_after_filter"] == 0
    assert result["issues"] == []
    assert result["truncated"] is False


_REAL_REPORT_LIB = Path("/mnt/Linux/github-projects/Millennium-Dawn/tools/report_lib")


@pytest.mark.skipif(
    not _REAL_REPORT_LIB.is_dir(), reason="sibling Millennium Dawn checkout unavailable"
)
def test_validate_delta_uses_real_sibling_report_lib(tmp_path):
    mod_root = _REAL_REPORT_LIB.parent.parent
    settings = Settings(mod_root=mod_root, vanilla_path=None, cache_dir=tmp_path / "cache")
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        json.dumps(
            [
                {
                    "severity": "error",
                    "category": "synthetic",
                    "message": "known",
                    "file": "events/synthetic.txt",
                    "line": 3,
                }
            ]
        ),
        encoding="utf-8",
    )
    records = [
        (
            {
                "severity": "error",
                "category": "synthetic",
                "message": "known",
                "file": "events/synthetic.txt",
                "line": 3,
            },
            "synthetic",
        ),
        (
            {
                "severity": "warning",
                "category": "synthetic",
                "message": "new",
                "file": "events/synthetic.txt",
                "line": 4,
            },
            "synthetic",
        ),
    ]

    delta_helper = _delta_helper()
    new_issues = delta_helper(settings, records, str(baseline_file))
    baseline_dir = tmp_path / "baseline-sidecars"
    baseline_dir.mkdir()
    (baseline_dir / "synthetic.json").write_text(
        baseline_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    sidecar_new_issues = delta_helper(settings, records, str(baseline_dir))

    assert [issue["message"] for issue in new_issues] == ["new"]
    assert [issue["message"] for issue in sidecar_new_issues] == ["new"]
