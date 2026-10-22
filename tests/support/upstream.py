"""Stand-ins for the Millennium-Dawn `tools/` tree, planted into a fake mod root."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upstream"

ISSUE_CLASS = """
class _Issue:
    def __init__(self, **kw):
        self.kw = kw

    def to_dict(self):
        return dict(self.kw)
"""

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

_BASELINE_SOURCE = r"""
import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from . import models

META_FILENAME = "baseline-meta.json"

_LINE_REF = re.compile(r"(\.\w+:|\bline )\d+")


class Baseline:
    def __init__(self, meta, keys):
        self.meta = meta
        self.keys = keys


def issue_key(issue):
    if not issue.category or not issue.file or issue.line <= 0 or not issue.message:
        return None
    message = _LINE_REF.sub(r"\1#", issue.message)
    return (issue.severity, issue.category, issue.file, message)


def count_keys(issues):
    return Counter(key for key in map(issue_key, issues) if key is not None)


from .dedupe import dedupe


def classify(issues, baseline):
    unmatched = baseline.keys.copy()
    new_issues = []
    for issue in issues:
        key = issue_key(issue)
        if key is None or unmatched[key] <= 0:
            if key is not None:
                new_issues.append(issue)
        else:
            unmatched[key] -= 1
    return SimpleNamespace(new_issues=new_issues)


def load_issues(directory):
    issues = []
    for path in Path(directory).glob("*.json"):
        if path.name == META_FILENAME:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
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
    return Baseline(meta=meta, keys=count_keys(load_issues(directory)))
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


def plant_validator(mod_root: Path, name: str, source: str) -> None:
    (mod_root / "tools" / "validation" / f"validate_{name}.py").write_text(source, encoding="utf-8")


def plant_report_lib(mod_root: Path) -> None:
    lib_dir = mod_root / "tools" / "report_lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / "models.py").write_text(_MODELS_SOURCE, encoding="utf-8")
    (lib_dir / "baseline.py").write_text(_BASELINE_SOURCE, encoding="utf-8")
    (lib_dir / "dedupe.py").write_text(_DEDUPE_SOURCE, encoding="utf-8")


def plant_focus_scenario(mod_root: Path, real_mod_root: Optional[Path] = None) -> None:
    """Put the mod's scenario evaluator under `mod_root/tools`.

    The small stand-ins in `tests/fixtures/upstream` by default, or the real
    scripts copied from `real_mod_root`, to check the stand-ins against them.
    """
    if real_mod_root is None:
        report = FIXTURES / "focus_overlap_report.py"
        shared = FIXTURES / "focus_shared_utils.py"
    else:
        report = real_mod_root / "tools" / "analysis" / "focus_overlap_report.py"
        shared = real_mod_root / "tools" / "shared_utils.py"
    (mod_root / "tools" / "analysis").mkdir(parents=True, exist_ok=True)
    shutil.copy(report, mod_root / "tools" / "analysis" / "focus_overlap_report.py")
    shutil.copy(shared, mod_root / "tools" / "shared_utils.py")


def make_script(root: Path, rel: str, body: str) -> Path:
    """Write an executable Python script at root/rel. Returns the path."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


def seed_all_scripts(root: Path, body_map: dict[str, str]) -> None:
    """Create stub scripts for every linter, defaulting to clean (exit 0, no output)."""
    defaults = {
        "tools/linting/check_common_mistakes.py": "import sys\nsys.exit(0)\n",
        "tools/linting/validate_mod_encoding.py": "import sys\nsys.exit(0)\n",
        "tools/linting/validate_localization_encoding.py": "import sys\nsys.exit(0)\n",
    }
    defaults.update(body_map)
    for rel, body in defaults.items():
        make_script(root, rel, body)
