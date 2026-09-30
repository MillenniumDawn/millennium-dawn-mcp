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
    prepare_baseline,
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
import json
from pathlib import Path
from types import SimpleNamespace

from . import models

META_FILENAME = "baseline-meta.json"


class Baseline:
    def __init__(self, meta, keys):
        self.meta = meta
        self.keys = keys


def issue_key(issue):
    if not issue.category or not issue.file or issue.line <= 0 or not issue.message:
        return None
    return (issue.severity, issue.category, issue.file, issue.line, issue.message)


from .dedupe import dedupe


def classify(issues, baseline):
    new_issues = [
        issue
        for issue in issues
        if issue_key(issue) is not None and issue_key(issue) not in baseline.keys
    ]
    return SimpleNamespace(new_issues=new_issues)


def load_issues(directory):
    issues = []
    for path in Path(directory).glob("*.json"):
        if path.name == META_FILENAME:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            issues.extend(models.Issue.from_dict(item, validator=path.stem) for item in data)
    return dedupe(issues)


def load_baseline(directory, expected_toolshash=None):
    base = Path(directory)
    meta_path = base / META_FILENAME
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    if expected_toolshash and meta.get("toolshash") != expected_toolshash:
        return None
    deduped = load_issues(directory)
    keys = {issue_key(issue) for issue in deduped if issue_key(issue) is not None}
    return Baseline(meta=meta, keys=keys)
"""

_DEDUPE_SOURCE = """
def dedupe(issues):
    merged = {}
    for issue in issues:
        key = (issue.category, issue.file, issue.line, issue.message)
        kept = merged.setdefault(key, issue)
        if kept is issue:
            continue
        if issue.validator not in (kept.validator, *kept.detected_by):
            kept.detected_by.append(issue.validator)
        if issue.severity == "error" and kept.severity == "warning":
            kept.severity = "error"
    return list(merged.values())
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
    (snapshot_dir / "baseline-meta.json").write_text("{}", encoding="utf-8")

    issues, meta = _load_baseline_issues(_settings(fake_mod_root), str(snapshot_dir), lib)

    assert meta == {}
    assert [i.message for i in issues] == ["m"]


def test_load_baseline_issues_accepts_snapshot_file(fake_mod_root: Path, tmp_path: Path):
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)
    snapshot = tmp_path / "baseline.json"
    single = [{"severity": "error", "category": "c", "message": "m", "file": "f", "line": 2}]
    snapshot.write_text(json.dumps(single), encoding="utf-8")

    issues, _meta = _load_baseline_issues(_settings(fake_mod_root), str(snapshot), lib)
    assert [i.message for i in issues] == ["m"]


def test_load_baseline_issues_missing_absolute_path_raises(fake_mod_root: Path, tmp_path: Path):
    """Absolute paths that don't exist are caller typos, not missing refs."""
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

    issues, _meta = _load_baseline_issues(settings, "feature-x", lib)
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
    result = validate_tool(settings, runner, delta=True, baseline="main")

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

    new_issues = new_issue_dicts(settings, records, str(snapshot)).issues

    assert [issue["message"] for issue in new_issues] == ["fresh"]


def _sidecar_issue(file: str, message: str = "known", line: int = 3) -> dict:
    return {
        "severity": "warning",
        "category": "fake",
        "message": message,
        "file": file,
        "line": line,
    }


def test_new_issue_dicts_matches_bare_sidecar_path_to_attributed_current_path(
    fake_mod_root: Path, tmp_path: Path
):
    _plant_report_lib(fake_mod_root)
    (fake_mod_root / "events").mkdir(exist_ok=True)
    (fake_mod_root / "events" / "a.txt").write_text("synthetic", encoding="utf-8")
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    (sidecars / "fake.json").write_text(json.dumps([_sidecar_issue("a.txt")]), encoding="utf-8")
    (sidecars / "baseline-meta.json").write_text("{}", encoding="utf-8")

    result = new_issue_dicts(
        _settings(fake_mod_root), [(_sidecar_issue("a.txt"), "fake")], str(sidecars)
    )

    assert result.issues == []
    assert result.unclassified == 0


def test_new_issue_dicts_keeps_ambiguous_basename_unclassified(fake_mod_root: Path, tmp_path: Path):
    _plant_report_lib(fake_mod_root)
    for directory in ("events", "common"):
        (fake_mod_root / directory).mkdir(exist_ok=True)
        (fake_mod_root / directory / "dup.txt").write_text("synthetic", encoding="utf-8")
    snapshot = tmp_path / "baseline.json"
    snapshot.write_text("[]", encoding="utf-8")

    result = new_issue_dicts(
        _settings(fake_mod_root), [(_sidecar_issue("dup.txt"), "fake")], str(snapshot)
    )

    assert result.issues == []
    assert result.unclassified == 1


# ----- reviewer feedback regressions -----


def test_load_baseline_issues_slash_ref_falls_back_to_cache_lookup(
    fake_mod_root: Path, monkeypatch, tmp_path: Path
):
    """`origin/main` and `issue/21-...` contain `/` but are refs, not paths.

    The earlier implementation treated any string with a slash as a snapshot
    path and raised FileNotFoundError when neither file nor dir existed. The
    loader must try file/dir first, then resolve as a ref.
    """
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)
    settings = _settings(fake_mod_root)
    baseline_dir = settings.cache_dir / "validator-baselines"
    baseline_dir.mkdir(parents=True)
    payload = [{"severity": "error", "category": "c", "message": "ref-hit", "file": "f", "line": 1}]
    (baseline_dir / "origin_main.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "origin").mkdir()

    issues, _meta = _load_baseline_issues(settings, "origin/main", lib)
    assert [i.message for i in issues] == ["ref-hit"]


def test_load_baseline_issues_uses_existing_snapshot_dir_when_present(
    fake_mod_root: Path, tmp_path: Path
):
    """A real directory on disk wins over the ref lookup even if its name contains a slash."""
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)
    settings = _settings(fake_mod_root)
    sidecars = tmp_path / "real" / "sub"
    sidecars.mkdir(parents=True)
    (sidecars / "synth.json").write_text(
        json.dumps(
            [
                {
                    "severity": "error",
                    "category": "c",
                    "message": "ok",
                    "file": "f",
                    "line": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    (sidecars / "baseline-meta.json").write_text("{}", encoding="utf-8")

    issues, meta = _load_baseline_issues(settings, str(sidecars), lib)
    assert meta == {}
    assert [i.message for i in issues] == ["ok"]


def test_prepare_baseline_requires_explicit_baseline(fake_mod_root: Path):
    """Delta mode must error when no baseline is given; no implicit main."""
    _plant_report_lib(fake_mod_root)
    with pytest.raises(FileNotFoundError, match="Delta mode requires an explicit"):
        prepare_baseline(_settings(fake_mod_root), None)


def test_new_issue_dicts_line_zero_file_finding_matches_by_severity_category_file_message(
    fake_mod_root: Path, tmp_path: Path
):
    """File-level findings (line=0, file present) match by severity/category/file/message.

    A previous version of the upstream keyer returned None for line<=0, which
    landed file-level findings in `unclassified` despite carrying everything
    needed to match. The delta keyer drops line from the tuple for line=0 cases
    so they compare on the four real fields.
    """
    _plant_report_lib(fake_mod_root)
    (fake_mod_root / "events").mkdir(exist_ok=True)
    (fake_mod_root / "events" / "a.txt").write_text("synthetic", encoding="utf-8")
    snapshot = tmp_path / "baseline.json"
    snapshot.write_text(
        json.dumps(
            [
                {
                    "severity": "warning",
                    "category": "fake",
                    "message": "whole-file warning",
                    "file": "events/a.txt",
                    "line": 0,
                }
            ]
        ),
        encoding="utf-8",
    )
    records = [
        (
            {
                "severity": "warning",
                "category": "fake",
                "message": "whole-file warning",
                "file": "events/a.txt",
                "line": 0,
            },
            "fake",
        )
    ]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(snapshot))

    assert result.issues == []
    assert result.unclassified == 0


def test_new_issue_dicts_line_zero_file_finding_distinguishes_severity(
    fake_mod_root: Path, tmp_path: Path
):
    """A baseline file-level warning should NOT match a file-level error at the same file.

    Severity is still part of the line-zero key, so an existing warning that
    escalates to an error reads as NEW (the alarm direction).
    """
    _plant_report_lib(fake_mod_root)
    (fake_mod_root / "events").mkdir(exist_ok=True)
    (fake_mod_root / "events" / "a.txt").write_text("synthetic", encoding="utf-8")
    snapshot = tmp_path / "baseline.json"
    snapshot.write_text(
        json.dumps(
            [
                {
                    "severity": "warning",
                    "category": "fake",
                    "message": "m",
                    "file": "events/a.txt",
                    "line": 0,
                }
            ]
        ),
        encoding="utf-8",
    )
    records = [
        (
            {
                "severity": "error",
                "category": "fake",
                "message": "m",
                "file": "events/a.txt",
                "line": 0,
            },
            "fake",
        )
    ]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(snapshot))

    assert [issue["message"] for issue in result.issues] == ["m"]


def test_new_issue_dicts_line_zero_with_no_file_stays_unclassified(
    fake_mod_root: Path, tmp_path: Path
):
    """Findings with no file at all are unkeyable in any direction and stay unclassified."""
    _plant_report_lib(fake_mod_root)
    snapshot = tmp_path / "baseline.json"
    snapshot.write_text("[]", encoding="utf-8")
    issue = {
        "severity": "warning",
        "category": "fake",
        "message": "no location",
        "file": "",
        "line": 0,
    }

    result = new_issue_dicts(_settings(fake_mod_root), [(issue, "fake")], str(snapshot))

    assert result.issues == []
    assert result.unclassified == 1


def test_load_baseline_issues_sidecar_dir_uses_upstream_load_baseline(
    fake_mod_root: Path, tmp_path: Path, monkeypatch
):
    """Sidecar dir loading goes through upstream `load_baseline` so meta is respected.

    The fixture report_lib defines a `load_baseline` that returns None when no
    meta file exists. The loader surfaces that as an actionable FileNotFoundError.
    """
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    (sidecars / "synth.json").write_text(
        json.dumps(
            [
                {
                    "severity": "error",
                    "category": "c",
                    "message": "m",
                    "file": "f",
                    "line": 1,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="baseline-meta.json"):
        _load_baseline_issues(_settings(fake_mod_root), str(sidecars), lib)


def test_load_baseline_issues_sidecar_dir_toolshash_mismatch_raises(
    fake_mod_root: Path, tmp_path: Path
):
    """A meta toolshash that disagrees with the current generation must error out.

    Without this check, every output-shape change between generations reads as
    NEW, which would silently break delta comparisons against stale baselines.
    """
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    (sidecars / "synth.json").write_text(
        json.dumps(
            [
                {
                    "severity": "error",
                    "category": "c",
                    "message": "m",
                    "file": "f",
                    "line": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    # Plant a toolshash that no real validator generation could produce.
    (sidecars / "baseline-meta.json").write_text(
        json.dumps({"toolshash": "deadbeef-deadbeef-deadbeef-deadbeefdeadbeef"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Baseline toolshash mismatch"):
        _load_baseline_issues(_settings(fake_mod_root), str(sidecars), lib)


def test_load_baseline_issues_sidecar_dir_matching_toolshash_loads_keys(
    fake_mod_root: Path, tmp_path: Path
):
    """When the meta's toolshash matches the locally recomputed one, load proceeds."""
    _plant_report_lib(fake_mod_root)
    lib = _report_lib(fake_mod_root)
    settings = _settings(fake_mod_root)
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    (sidecars / "synth.json").write_text(
        json.dumps(
            [
                {
                    "severity": "error",
                    "category": "c",
                    "message": "m",
                    "file": "f",
                    "line": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    current = issue_delta._compute_toolshash(settings.mod_root)
    (sidecars / "baseline-meta.json").write_text(
        json.dumps({"toolshash": current}), encoding="utf-8"
    )

    issues, meta = _load_baseline_issues(settings, str(sidecars), lib)

    assert meta == {"toolshash": current}
    assert len(issues) == 1


# ----- toolshash independent compatibility vector -----

# A controlled fixture whose expected hash is computed independently from the
# @actions/glob `internal-hash-files` algorithm: per-file SHA-256 (binary
# digest), concatenate, then SHA-256 again. Files are visited in alphasort
# DFS order, matching Node's `fs.readdir` `alphasort` on Linux.
_INDEPENDENT_HASH = "2bc448509b3496d8756d101679ce458cc10293eae507981266fdeaea80937bc6"


def test_compute_toolshash_matches_independent_vector(tmp_path: Path):
    mod_root = tmp_path / "mod"
    (mod_root / "tools" / "validation" / "sub").mkdir(parents=True)
    (mod_root / "tools" / "shared_utils.py").write_text("shared", encoding="utf-8")
    (mod_root / "tools" / "validation" / "a.py").write_text("alpha", encoding="utf-8")
    (mod_root / "tools" / "validation" / "b.py").write_text("beta", encoding="utf-8")
    (mod_root / "tools" / "validation" / "sub" / "c.py").write_text("gamma", encoding="utf-8")
    (mod_root / "validation_config.json").write_text("{}", encoding="utf-8")

    assert issue_delta._compute_toolshash(mod_root) == _INDEPENDENT_HASH


def test_compute_toolshash_ignores_python_cache_files(tmp_path: Path):
    validation = tmp_path / "tools" / "validation"
    validation.mkdir(parents=True)
    (validation / "validate_sample.py").write_text("sample", encoding="utf-8")
    before = issue_delta._compute_toolshash(tmp_path)
    cache = validation / "__pycache__"
    cache.mkdir()
    (cache / "validate_sample.cpython-310.pyc").write_bytes(b"cached")
    (validation / "legacy.pyc").write_bytes(b"cached")

    assert issue_delta._compute_toolshash(tmp_path) == before


def test_compute_toolshash_no_matching_files_returns_empty(tmp_path: Path):
    assert issue_delta._compute_toolshash(tmp_path) == ""


def test_compute_toolshash_changes_when_a_file_changes(tmp_path: Path):
    mod_root = tmp_path / "mod"
    (mod_root / "tools" / "validation").mkdir(parents=True)
    (mod_root / "tools" / "shared_utils.py").write_text("shared", encoding="utf-8")
    (mod_root / "tools" / "validation" / "a.py").write_text("alpha", encoding="utf-8")
    (mod_root / "validation_config.json").write_text("{}", encoding="utf-8")

    before = issue_delta._compute_toolshash(mod_root)

    (mod_root / "tools" / "validation" / "a.py").write_text("alpha2", encoding="utf-8")
    assert issue_delta._compute_toolshash(mod_root) != before


def test_compute_toolshash_changes_when_a_file_is_added(tmp_path: Path):
    mod_root = tmp_path / "mod"
    (mod_root / "tools" / "validation").mkdir(parents=True)
    (mod_root / "tools" / "shared_utils.py").write_text("shared", encoding="utf-8")
    (mod_root / "validation_config.json").write_text("{}", encoding="utf-8")

    before = issue_delta._compute_toolshash(mod_root)

    (mod_root / "tools" / "validation" / "new.py").write_text("new", encoding="utf-8")
    assert issue_delta._compute_toolshash(mod_root) != before


# ----- sidecar line-zero + cross-validator dedupe regressions -----


def test_new_issue_dicts_unchanged_line_zero_sidecar_finding_matches(
    fake_mod_root: Path, tmp_path: Path
):
    """An unchanged line-zero sidecar finding must not read as NEW on the next run.

    Upstream `load_baseline.keys` filters out line<=0 findings, so a literal
    reuse of those keys would leave every file-level sidecar finding tagged
    NEW forever. The loader uses `load_issues` for the actual issue list
    and runs them through the same `_delta_key` path as the snapshot branch,
    so they compare cleanly.
    """
    _plant_report_lib(fake_mod_root)
    (fake_mod_root / "events").mkdir(exist_ok=True)
    (fake_mod_root / "events" / "a.txt").write_text("synthetic", encoding="utf-8")
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    (sidecars / "synth.json").write_text(
        json.dumps(
            [
                {
                    "severity": "warning",
                    "category": "fake",
                    "message": "whole-file warning",
                    "file": "a.txt",
                    "line": 0,
                }
            ]
        ),
        encoding="utf-8",
    )
    (sidecars / "baseline-meta.json").write_text("{}", encoding="utf-8")

    records = [
        (
            {
                "severity": "warning",
                "category": "fake",
                "message": "whole-file warning",
                "file": "a.txt",
                "line": 0,
            },
            "fake",
        )
    ]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(sidecars))

    assert result.issues == []
    assert result.unclassified == 0


def test_new_issue_dicts_mixed_severity_duplicate_baseline_dedupes(
    fake_mod_root: Path, tmp_path: Path
):
    """Both sides dedupe duplicate findings to the same escalated severity."""
    _plant_report_lib(fake_mod_root)
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    (sidecars / "validator_a.json").write_text(
        json.dumps(
            [
                {
                    "severity": "warning",
                    "category": "fake",
                    "message": "shared finding",
                    "file": "events/a.txt",
                    "line": 5,
                }
            ]
        ),
        encoding="utf-8",
    )
    (sidecars / "validator_b.json").write_text(
        json.dumps(
            [
                {
                    "severity": "error",
                    "category": "fake",
                    "message": "shared finding",
                    "file": "events/a.txt",
                    "line": 5,
                }
            ]
        ),
        encoding="utf-8",
    )
    (sidecars / "baseline-meta.json").write_text("{}", encoding="utf-8")

    records = [
        (
            {
                "severity": "warning",
                "category": "fake",
                "message": "shared finding",
                "file": "events/a.txt",
                "line": 5,
            },
            "alpha",
        ),
        (
            {
                "severity": "error",
                "category": "fake",
                "message": "shared finding",
                "file": "events/a.txt",
                "line": 5,
            },
            "beta",
        ),
    ]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(sidecars))

    assert result.issues == []
    assert result.owners == []
    assert result.unclassified == 0


def test_new_issue_dicts_baseline_keeps_only_escalated_key(fake_mod_root: Path, tmp_path: Path):
    """A warning-only subset differs from an error-escalated baseline, as upstream does."""
    _plant_report_lib(fake_mod_root)
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    (sidecars / "validator_a.json").write_text(
        json.dumps(
            [
                {
                    "severity": "warning",
                    "category": "fake",
                    "message": "shared",
                    "file": "events/a.txt",
                    "line": 5,
                }
            ]
        ),
        encoding="utf-8",
    )
    (sidecars / "validator_b.json").write_text(
        json.dumps(
            [
                {
                    "severity": "error",
                    "category": "fake",
                    "message": "shared",
                    "file": "events/a.txt",
                    "line": 5,
                }
            ]
        ),
        encoding="utf-8",
    )
    (sidecars / "baseline-meta.json").write_text("{}", encoding="utf-8")

    records = [
        (
            {
                "severity": "warning",
                "category": "fake",
                "message": "shared",
                "file": "events/a.txt",
                "line": 5,
            },
            "alpha",
        )
    ]

    prepared = prepare_baseline(_settings(fake_mod_root), str(sidecars))
    assert prepared.keys == {("error", "fake", "events/a.txt", 5, "shared")}
    result = new_issue_dicts(
        _settings(fake_mod_root), records, str(sidecars), prepared_baseline=prepared
    )

    assert len(result.issues) == 1
    assert result.issues[0]["severity"] == "warning"
    assert result.owners == ["alpha"]
    assert result.unclassified == 0
