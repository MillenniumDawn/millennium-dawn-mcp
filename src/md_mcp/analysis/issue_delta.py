"""Compare validator findings with a baseline using the mod's report library."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NamedTuple, Optional

from ..validators.attribution import IssueAttributor

_TOOLSHASH_PATTERNS = (
    "tools/validation/**",
    "tools/shared_utils.py",
    "validation_config.json",
)


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
        load_baseline=baseline.load_baseline,
        META_FILENAME=baseline.META_FILENAME,
    )


def _safe_ref(ref: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", ref).strip("_")
    return "_" if safe in {"", ".", ".."} else safe


def _iter_glob_files(mod_root: Path) -> list[Path]:
    """Visit validator sources in the Linux runner's depth-first file order."""
    files: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            return
        for entry in entries:
            child = directory / entry
            # Upstream hashes before Python creates bytecode caches.
            if entry == "__pycache__" or child.suffix in {".pyc", ".pyo"}:
                continue
            if child.is_file():
                files.append(child)
            elif child.is_dir():
                visit(child)

    for pattern in _TOOLSHASH_PATTERNS:
        if "**" in pattern:
            base = mod_root / pattern.split("**", 1)[0].rstrip("/")
            if base.is_dir():
                visit(base)
        else:
            target = mod_root / pattern
            if target.is_file():
                files.append(target)

    return files


def _compute_toolshash(mod_root: Path) -> str:
    """Hash the concatenated file digests, as GitHub Actions `hashFiles` does."""
    files = _iter_glob_files(mod_root)
    final = hashlib.sha256()
    for path in files:
        final.update(hashlib.sha256(path.read_bytes()).digest())
    return final.hexdigest() if files else ""


def _load_baseline_issues(settings, baseline: Optional[str], report_lib) -> tuple[list, dict]:
    """Resolve `baseline` to a deduped list of `Issue` objects plus sidecar meta."""
    if baseline and (
        Path(baseline).is_absolute()
        or "/" in baseline
        or "\\" in baseline
        or baseline.endswith(".json")
    ):
        candidate = Path(baseline)
        if candidate.is_dir():
            issues, meta = _load_sidecar_dir(settings, candidate, report_lib)
            return issues, meta
        if candidate.is_file():
            return _read_issue_file(candidate, report_lib.Issue), {}
        if candidate.is_absolute():
            raise FileNotFoundError(
                f"Baseline snapshot not found: {candidate}. Pass a snapshot file or directory."
            )

    if not baseline:
        raise FileNotFoundError(
            "Delta mode requires an explicit `baseline` (snapshot file, sidecar "
            "directory, or git ref). Pass one via the `baseline` argument."
        )

    snapshot = settings.cache_dir / "validator-baselines" / f"{_safe_ref(baseline)}.json"
    if not snapshot.is_file():
        raise FileNotFoundError(
            f"Baseline snapshot not found: {snapshot}. Pass a snapshot file or directory."
        )
    return _read_issue_file(snapshot, report_lib.Issue), {}


def _load_sidecar_dir(settings, candidate: Path, report_lib) -> tuple[list, dict]:
    """Check sidecar metadata, then load issues without dropping file-level findings."""
    loaded = report_lib.load_baseline(str(candidate))
    if loaded is None:
        raise FileNotFoundError(
            f"Baseline directory {candidate} is missing or unreadable: it must "
            f"contain {report_lib.META_FILENAME} produced by upstream "
            f"baseline_check.py."
        )
    stored_hash = loaded.meta.get("toolshash") if isinstance(loaded.meta, dict) else None
    if stored_hash:
        current_hash = _compute_toolshash(settings.mod_root)
        if stored_hash != current_hash:
            raise ValueError(
                f"Baseline toolshash mismatch in {candidate / report_lib.META_FILENAME}: "
                f"baseline was built with {stored_hash!r} but current validator "
                f"generation hashes to {current_hash!r}. Re-establish the baseline "
                f"for the current toolshash before using it."
            )
    return report_lib.load_issues(str(candidate)), loaded.meta


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


def _delta_key(issue) -> Optional[tuple]:
    """Keep severity in the key and omit absent line numbers."""
    if not issue.category or not issue.file or not issue.message:
        return None
    if issue.line > 0:
        return (issue.severity, issue.category, issue.file, issue.line, issue.message)
    return (issue.severity, issue.category, issue.file, issue.message)


def _dedupe_key(issue) -> tuple:
    """Match a deduped finding to the validator that reported its severity."""
    if issue.line > 0:
        return (issue.category, issue.file, issue.line, issue.message)
    return (issue.category, issue.file, issue.message)


@dataclass(frozen=True)
class PreparedBaseline:
    report_lib: Any
    keys: set
    attributor: IssueAttributor


class DeltaResult(NamedTuple):
    issues: list[dict]
    owners: list[str]
    unclassified: int


def _attribute(issue, attributor: IssueAttributor) -> None:
    issue.file = attributor.resolve({"file": issue.file, "message": issue.message}) or ""


def prepare_baseline(settings, baseline: Optional[str]) -> PreparedBaseline:
    report_lib = _report_lib(settings.mod_root)
    attributor = IssueAttributor(settings.mod_root)
    raw_issues, _ = _load_baseline_issues(settings, baseline, report_lib)
    for issue in raw_issues:
        _attribute(issue, attributor)
    deduped = report_lib.dedupe(raw_issues)
    baseline_keys = {key for issue in deduped if (key := _delta_key(issue)) is not None}
    return PreparedBaseline(report_lib=report_lib, keys=baseline_keys, attributor=attributor)


def _owner(issue, reported: dict[tuple, list[tuple[str, str]]]) -> str:
    key = _dedupe_key(issue)
    for validator, severity in reported.get(key, ()):
        if severity == issue.severity:
            return validator
    return issue.validator


def _classify(current_issues: list, baseline_keys: set) -> tuple[list, int]:
    new_issues = []
    unclassified = 0
    for issue in current_issues:
        key = _delta_key(issue)
        if key is None:
            unclassified += 1
        elif key not in baseline_keys:
            new_issues.append(issue)
    return new_issues, unclassified


def new_issue_dicts(
    settings,
    issue_records: list[tuple[dict, str]],
    baseline: Optional[str],
    *,
    prepared_baseline: Optional[PreparedBaseline] = None,
) -> DeltaResult:
    prepared = prepared_baseline or prepare_baseline(settings, baseline)
    report_lib = prepared.report_lib
    current_issues = []
    reported: dict[tuple, list[tuple[str, str]]] = {}
    for issue_dict, validator in issue_records:
        issue = report_lib.Issue.from_dict(issue_dict, validator=validator)
        _attribute(issue, prepared.attributor)
        current_issues.append(issue)
        reported.setdefault(_dedupe_key(issue), []).append((issue.validator, issue.severity))

    current_issues = report_lib.dedupe(current_issues)
    new_issues, unclassified = _classify(current_issues, prepared.keys)
    return DeltaResult(
        issues=[issue.to_dict() for issue in new_issues],
        owners=[_owner(issue, reported) for issue in new_issues],
        unclassified=unclassified,
    )
