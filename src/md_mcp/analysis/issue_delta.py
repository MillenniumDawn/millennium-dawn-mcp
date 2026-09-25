"""Compare validator findings with a baseline using the mod's report library."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Optional

from ..validators.attribution import IssueAttributor


def _report_lib(mod_root: Path):
    """Load report_lib modules from this mod checkout without importing its CLI."""
    report_dir = Path(mod_root) / "tools" / "report_lib"
    if not report_dir.is_dir():
        raise ImportError(f"report_lib not found under {report_dir.parent}")

    package_name = (
        "_md_mcp_report_lib_"
        + hashlib.sha256(str(report_dir.resolve()).encode("utf-8")).hexdigest()
    )
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(report_dir)]
        package.__package__ = package_name
        sys.modules[package_name] = package

    models = importlib.import_module(f"{package_name}.models")
    dedupe = importlib.import_module(f"{package_name}.dedupe")
    baseline = importlib.import_module(f"{package_name}.baseline")
    return SimpleNamespace(
        Issue=models.Issue,
        Baseline=baseline.Baseline,
        classify=baseline.classify,
        dedupe=dedupe.dedupe,
        issue_key=baseline.issue_key,
        load_issues=baseline.load_issues,
    )


def _safe_ref(ref: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", ref).strip("_")
    return "_" if safe in {"", ".", ".."} else safe


def _load_baseline_issues(settings, baseline: Optional[str], report_lib) -> list:
    if baseline is not None:
        candidate = Path(baseline)
        is_snapshot_path = (
            candidate.is_absolute()
            or "/" in baseline
            or "\\" in baseline
            or baseline.endswith(".json")
        )
        if is_snapshot_path:
            if candidate.is_dir():
                return report_lib.load_issues(str(candidate))
            if candidate.is_file():
                return _read_issue_file(candidate, report_lib.Issue)
            raise FileNotFoundError(
                f"Baseline snapshot not found: {candidate}. Pass a snapshot file or directory."
            )

    ref = baseline or "main"
    snapshot = settings.cache_dir / "validator-baselines" / f"{_safe_ref(ref)}.json"
    if not snapshot.is_file():
        raise FileNotFoundError(
            f"Baseline snapshot not found: {snapshot}. Pass a snapshot file or directory."
        )
    return _read_issue_file(snapshot, report_lib.Issue)


def _read_issue_file(path: Path, issue_type) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read baseline snapshot {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(
            f"Baseline snapshot {path} must contain a JSON list of issue dictionaries."
        )
    return [issue_type.from_dict(item) for item in data if isinstance(item, dict)]


@dataclass(frozen=True)
class PreparedBaseline:
    report_lib: Any
    keys: set


def prepare_baseline(settings, baseline: Optional[str]) -> PreparedBaseline:
    """Load and key the baseline before running validators."""
    report_lib = _report_lib(settings.mod_root)
    baseline_issues = report_lib.dedupe(_load_baseline_issues(settings, baseline, report_lib))
    baseline_keys = {
        key for issue in baseline_issues if (key := report_lib.issue_key(issue)) is not None
    }
    return PreparedBaseline(report_lib=report_lib, keys=baseline_keys)


def new_issue_dicts(
    settings,
    issue_records: list[tuple[dict, str]],
    baseline: Optional[str],
    *,
    prepared_baseline: Optional[PreparedBaseline] = None,
) -> list[dict]:
    """Return deduped current findings classified as new against a snapshot."""
    prepared = prepared_baseline or prepare_baseline(settings, baseline)
    report_lib = prepared.report_lib
    attributor = IssueAttributor(settings.mod_root)
    current_issues = []
    for issue_dict, validator in issue_records:
        normalized = dict(issue_dict)
        normalized["file"] = attributor.resolve(normalized) or ""
        current_issues.append(report_lib.Issue.from_dict(normalized, validator=validator))

    current_issues = report_lib.dedupe(current_issues)
    current_baseline = report_lib.Baseline(meta={}, keys=prepared.keys)
    stats = report_lib.classify(current_issues, current_baseline)
    new_issues = stats.new_issues + [
        issue for issue in current_issues if report_lib.issue_key(issue) is None
    ]
    return [issue.to_dict() for issue in new_issues]
