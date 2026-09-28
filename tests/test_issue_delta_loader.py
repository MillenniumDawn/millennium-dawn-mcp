"""Unit coverage for the delta baseline loader and its failure branches.

`tests/test_validation_tools.py` covers the delta flow with the report lib
monkeypatched out; these tests plant a synthetic `tools/report_lib/` so the
real loader, baseline resolution, and snapshot parsing run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from md_mcp.analysis import issue_delta
from md_mcp.analysis.issue_delta import (
    _load_baseline_issues,
    _read_issue_file,
    _report_lib,
    _safe_ref,
    new_issue_dicts,
)
from md_mcp.config import Settings
from md_mcp.tools.validation_tools import validate_tool

_MODELS_SOURCE = """
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
"""

_BASELINE_SOURCE = """
from types import SimpleNamespace

from . import models


class Baseline:
    def __init__(self, meta, keys):
        self.meta = meta
        self.keys = keys


def issue_key(issue):
    if not issue.category or not issue.file or issue.line <= 0 or not issue.message:
        return None
    return (issue.severity, issue.category, issue.file, issue.line, issue.message)


def dedupe(issues):
    key = lambda issue: (issue.category, issue.file, issue.line, issue.message)
    return list({key(issue): issue for issue in issues}.values())


def classify(issues, baseline):
    new_issues = [
        issue
        for issue in issues
        if issue_key(issue) is not None and issue_key(issue) not in baseline.keys
    ]
    return SimpleNamespace(new_issues=new_issues)


def load_issues(directory):
    import json
    from pathlib import Path

    from . import models

    issues = []
    for path in Path(directory).glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            issues.extend(models.Issue.from_dict(item, validator=path.stem) for item in data)
    return dedupe(issues)
"""

_DEDUPE_SOURCE = """
def dedupe(issues):
    key = lambda issue: (issue.category, issue.file, issue.line, issue.message)
    return list({key(issue): issue for issue in issues}.values())
"""

_VALIDATOR_SOURCE = """
class Validator:
    TITLE = "Allp"

    def __init__(self, mod_path, output_file=None, use_colors=True, staged_only=False, **kw):
        self._issues = []

    def run_all_validations(self):
        self._issues = []
"""


@pytest.fixture(autouse=True)
def _drop_cached_report_lib_packages():
    yield
    for name in list(sys.modules):
        if name.startswith("_md_mcp_report_lib_"):
            sys.modules.pop(name, None)


def _plant_report_lib(mod_root: Path) -> None:
    lib_dir = mod_root / "tools" / "report_lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / "models.py").write_text(_MODELS_SOURCE, encoding="utf-8")
    (lib_dir / "baseline.py").write_text(_BASELINE_SOURCE, encoding="utf-8")
    (lib_dir / "dedupe.py").write_text(_DEDUPE_SOURCE, encoding="utf-8")


def _settings(mod_root: Path) -> Settings:
    return Settings(mod_root=mod_root, vanilla_path=None, cache_dir=mod_root / ".md-mcp-cache")


class _FakeRunner:
    def __init__(self, issues):
        self.issues = issues

    def run(self, validator, **kwargs):
        return {
            "ok": True,
            "validator": validator,
            "counts": {"error": 1, "warning": 0, "info": 0},
            "issues": self.issues,
        }


def test_report_lib_loads_models_dedupe_and_baseline(fake_mod_root: Path):
    _plant_report_lib(fake_mod_root)

    lib = _report_lib(fake_mod_root)

    assert lib.Issue.from_dict({"severity": "warning"}).severity == "warning"
    assert callable(lib.dedupe)
    assert callable(lib.classify)
    assert callable(lib.issue_key)
    assert callable(lib.load_issues)

    second = _report_lib(fake_mod_root)
    assert second.Issue is lib.Issue


def test_report_lib_missing_directory_raises(fake_mod_root: Path):
    with pytest.raises(ImportError, match="report_lib not found"):
        _report_lib(fake_mod_root)


def test_safe_ref_sanitizes_hostile_refs():
    assert _safe_ref("feature/x") == "feature_x"
    assert _safe_ref("") == "_"
    assert _safe_ref("..") == "_"
    assert _safe_ref("main") == "main"


def test_load_baseline_issues_accepts_snapshot_directory(fake_mod_root: Path, tmp_path: Path):
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    one = [{"severity": "warning", "category": "c", "message": "m", "file": "f", "line": 1}]
    (snapshot_dir / "one.json").write_text(json.dumps(one), encoding="utf-8")
    (snapshot_dir / "junk.json").write_text('{"not": "a list"}', encoding="utf-8")

    issues = _load_baseline_issues(_settings(fake_mod_root), str(snapshot_dir), lib)

    assert [i.message for i in issues] == ["m"]
    assert issues[0].validator == "one"


def test_load_baseline_issues_accepts_snapshot_file(fake_mod_root: Path, tmp_path: Path):
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)
    snapshot = tmp_path / "baseline.json"
    single = [{"severity": "error", "category": "c", "message": "m", "file": "f", "line": 2}]
    snapshot.write_text(json.dumps(single), encoding="utf-8")

    issues = _load_baseline_issues(_settings(fake_mod_root), str(snapshot), lib)

    assert [i.message for i in issues] == ["m"]


def test_load_baseline_issues_missing_absolute_path_raises(fake_mod_root: Path, tmp_path: Path):
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)

    with pytest.raises(FileNotFoundError, match="Baseline snapshot not found"):
        _load_baseline_issues(_settings(fake_mod_root), str(tmp_path / "gone.json"), lib)


def test_load_baseline_issues_resolves_named_refs_from_cache(fake_mod_root: Path):
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)
    settings = _settings(fake_mod_root)
    (settings.cache_dir / "validator-baselines").mkdir(parents=True)
    snapshot = settings.cache_dir / "validator-baselines" / "feature-x.json"
    ref = [{"severity": "warning", "category": "c", "message": "m", "file": "f", "line": 5}]
    snapshot.write_text(json.dumps(ref), encoding="utf-8")

    issues = _load_baseline_issues(settings, "feature-x", lib)

    assert [i.line for i in issues] == [5]


def test_load_baseline_issues_missing_ref_raises(fake_mod_root: Path):
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)

    with pytest.raises(FileNotFoundError, match="Baseline snapshot not found"):
        _load_baseline_issues(_settings(fake_mod_root), "nope", lib)


def test_read_issue_file_rejects_unreadable_and_malformed_payloads(tmp_path: Path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="Could not read baseline snapshot"):
        _read_issue_file(bad_json, _StubIssue)

    not_a_list = tmp_path / "obj.json"
    not_a_list.write_text('{"severity": "error"}', encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON list"):
        _read_issue_file(not_a_list, _StubIssue)


class _StubIssue:
    """Minimal issue_type so _read_issue_file's from_dict loop is observable."""

    @classmethod
    def from_dict(cls, item):
        return item


def test_read_issue_file_skips_non_dict_entries(tmp_path: Path):
    payload = tmp_path / "mixed.json"
    payload.write_text(json.dumps([{"ok": True}, 42, {"severity": "warning"}]), encoding="utf-8")

    issues = _read_issue_file(payload, _StubIssue)

    assert issues == [{"ok": True}, {"severity": "warning"}]


def _poisoned_issue() -> dict:
    # `line="boom"` makes the synthetic Issue.from_dict raise inside
    # new_issue_dicts, after the baseline prepared fine.
    return {
        "severity": "warning",
        "category": "fake",
        "message": "m1",
        "file": "events/a.txt",
        "line": "boom",
    }


def test_validate_delta_surfaces_new_issue_dicts_failure_for_single_validator(
    fake_mod_root: Path, monkeypatch
):
    _plant_report_lib(fake_mod_root)
    settings = _settings(fake_mod_root)
    snapshot = settings.cache_dir / "validator-baselines" / "main.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps([]), encoding="utf-8")
    monkeypatch.setattr(issue_delta, "_report_lib", lambda _root: _report_lib(fake_mod_root))

    runner = cast("Any", _FakeRunner([_poisoned_issue()]))
    result = validate_tool(settings, runner, validator="fake", delta=True, baseline="main")

    assert result["ok"] is False
    assert "invalid literal for int" in result["error"]


def _plant_simple_validator(mod_root: Path, name: str) -> None:
    (mod_root / "tools" / "validation" / f"validate_{name}.py").write_text(
        _VALIDATOR_SOURCE, encoding="utf-8"
    )


def test_validate_delta_surfaces_new_issue_dicts_failure_for_all_validators(
    fake_mod_root: Path, monkeypatch
):
    _plant_report_lib(fake_mod_root)
    _plant_simple_validator(fake_mod_root, "allp")
    settings = _settings(fake_mod_root)
    snapshot = settings.cache_dir / "validator-baselines" / "main.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps([]), encoding="utf-8")
    monkeypatch.setattr(issue_delta, "_report_lib", lambda _root: _report_lib(fake_mod_root))

    runner = cast("Any", _FakeRunner([_poisoned_issue()]))
    result = validate_tool(settings, runner, delta=True)

    assert result["ok"] is False
    assert "invalid literal for int" in result["error"]


def test_new_issue_dicts_classifies_against_planted_report_lib(fake_mod_root: Path, tmp_path: Path):
    _plant_report_lib(fake_mod_root)
    (fake_mod_root / "events").mkdir(exist_ok=True)
    (fake_mod_root / "events" / "a.txt").write_text("synthetic", encoding="utf-8")
    settings = _settings(fake_mod_root)
    snapshot = tmp_path / "baseline.json"
    snapshot.write_text(
        json.dumps(
            [
                {
                    "severity": "warning",
                    "category": "fake",
                    "message": "known",
                    "file": "events/a.txt",
                    "line": 3,
                }
            ]
        ),
        encoding="utf-8",
    )
    records: list[tuple[dict, str]] = [
        (
            {
                "severity": "warning",
                "category": "fake",
                "message": "known",
                "file": "a.txt",
                "line": 3,
            },
            "fake",
        ),
        (
            {
                "severity": "error",
                "category": "fake",
                "message": "fresh",
                "file": "a.txt",
                "line": 9,
            },
            "fake",
        ),
    ]

    new_issues = new_issue_dicts(settings, records, str(snapshot))

    assert [issue["message"] for issue in new_issues] == ["fresh"]
