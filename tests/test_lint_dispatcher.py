"""Tests for the unified `lint` dispatcher + the secondary lint wrappers.

Strategy: write fake stand-in scripts at the expected paths and assert the
dispatcher routes correctly, parses each script's output flavour, aggregates
issues, and respects the `checks=[...]` / `severity_min=` / `limit=` /
`counts_only=` knobs.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from md_mcp.tools.linting_tools import (
    _ALL_CHECKS,
    lint_basic_style_2_tool,
    lint_coding_standards_tool,
    lint_loc_encoding_tool,
    lint_tool,
)


def _make_script(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _seed_all_scripts(root: Path, body_map: dict[str, str]) -> None:
    """Create stub scripts for every linter, defaulting to clean (exit 0, no output)."""
    defaults = {
        "tools/linting/check_common_mistakes.py": "import sys\nsys.exit(0)\n",
        "tools/linting/check_braces.py": "import sys\nsys.exit(0)\n",
        "tools/linting/check_basic_style.py": "import sys\nsys.exit(0)\n",
        "tools/linting/check_basic_style_2.py": "import sys\nsys.exit(0)\n",
        "tools/linting/coding_standards.py": "import sys\nsys.exit(0)\n",
        "tools/linting/validate_mod_encoding.py": "import sys\nsys.exit(0)\n",
        "tools/linting/validate_localization_encoding.py": "import sys\nsys.exit(0)\n",
    }
    defaults.update(body_map)
    for rel, body in defaults.items():
        _make_script(root, rel, body)


# ---------------------------------------------------------------------------
# Secondary wrapper signatures + happy paths
# ---------------------------------------------------------------------------


def test_secondary_wrapper_signatures():
    for fn, required in [
        (lint_basic_style_2_tool, ("mod_root", "mode", "files", "limit")),
        (lint_coding_standards_tool, ("mod_root", "mode", "limit")),
        (lint_loc_encoding_tool, ("mod_root", "files", "limit")),
    ]:
        params = inspect.signature(fn).parameters
        for p in required:
            assert p in params, f"{fn.__name__} missing param: {p}"


def test_basic_style_2_parses_warning_lines(tmp_path):
    _make_script(
        tmp_path,
        "tools/linting/check_basic_style_2.py",
        """import sys
print("Validating Basic Style - Secondary Check (Mode: all)")
print("WARNING: Missing a space before or after open brace at /tmp/x.txt Line number: 1")
print("WARNING: Missing a space before or after close brace at /tmp/x.txt Line number: 2")
sys.exit(1)
""",
    )
    out = lint_basic_style_2_tool(tmp_path, mode="all")
    assert out["ok"] is True
    assert out["total"] == 2
    assert all(i["severity"] == "warning" for i in out["issues"])
    assert out["issues"][0]["line"] == 1


def test_coding_standards_parses_warning_in_file(tmp_path):
    _make_script(
        tmp_path,
        "tools/linting/coding_standards.py",
        """import sys
print("Validating Coding Standards (Mode: all)")
print("WARNING: BadID is formatted incorrectly, must be TAG_focus_name in /tmp/x.txt Line number: 5")
sys.exit(0)
""",
    )
    out = lint_coding_standards_tool(tmp_path, mode="all")
    assert out["ok"] is True
    assert out["total"] == 1
    issue = out["issues"][0]
    assert issue["severity"] == "warning"
    assert issue["line"] == 5
    assert "BadID" in issue["message"]


def test_loc_encoding_parses_missing_bom(tmp_path):
    _make_script(
        tmp_path,
        "tools/linting/validate_localization_encoding.py",
        """import sys
print("localisation/english/foo_l_english.yml: Missing UTF-8 BOM (required for HOI4 localization)")
sys.exit(1)
""",
    )
    out = lint_loc_encoding_tool(tmp_path)
    assert out["ok"] is True
    assert out["total"] == 1
    assert "Missing UTF-8 BOM" in out["issues"][0]["message"]
    assert out["issues"][0]["severity"] == "error"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_lint_runs_every_check_by_default(tmp_path):
    _seed_all_scripts(tmp_path, {})
    (tmp_path / "descriptor.mod").write_text('name = "x"\n')
    out = lint_tool(tmp_path, mode="all")
    assert out["ok"] is True
    # Every check ran with ok=true, even when there's no work.
    names = {c["name"] for c in out["checks"]}
    assert names == set(_ALL_CHECKS)
    assert out["counts"] == {"error": 0, "warning": 0, "info": 0}


def test_lint_rejects_unknown_check(tmp_path):
    _seed_all_scripts(tmp_path, {})
    out = lint_tool(tmp_path, checks=["common_mistakes", "bogus"])
    assert out["ok"] is False
    assert "Unknown check(s)" in out["error"]


def test_lint_subset_only_runs_requested(tmp_path):
    _seed_all_scripts(tmp_path, {})
    out = lint_tool(tmp_path, checks=["braces", "coding_standards"], mode="all")
    assert out["ok"] is True
    ran = {c["name"] for c in out["checks"]}
    assert ran == {"braces", "coding_standards"}


def test_lint_aggregates_issues_from_multiple_checks(tmp_path):
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_braces.py": """import sys
print("a.txt:", file=sys.stderr)
print("  Line 1, Column 1: Opening brace '{' without matching closing brace", file=sys.stderr)
sys.exit(1)
""",
            "tools/linting/check_basic_style_2.py": """import sys
print("WARNING: Missing space at a.txt Line number: 1")
sys.exit(1)
""",
        },
    )
    out = lint_tool(tmp_path, checks=["braces", "basic_style_2"], files=["a.txt"])
    assert out["ok"] is True
    assert out["counts"]["error"] == 1
    assert out["counts"]["warning"] == 1
    # Each issue is tagged with which check produced it.
    by_check = {i["check"] for i in out["issues"]}
    assert by_check == {"braces", "basic_style_2"}


def test_lint_severity_floor_filters_warnings(tmp_path):
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_basic_style_2.py": """import sys
print("WARNING: foo at a.txt Line number: 1")
print("WARNING: bar at a.txt Line number: 2")
sys.exit(1)
""",
            "tools/linting/check_braces.py": """import sys
print("a.txt:", file=sys.stderr)
print("  Line 9, Column 1: stray brace", file=sys.stderr)
sys.exit(1)
""",
        },
    )
    out = lint_tool(
        tmp_path,
        checks=["braces", "basic_style_2"],
        files=["a.txt"],
        severity_min="error",
    )
    # Counts are pre-filter, issues are post-filter.
    assert out["counts"]["warning"] == 2
    assert out["counts"]["error"] == 1
    assert out["issues_total_after_filter"] == 1
    assert out["issues"][0]["severity"] == "error"


def test_lint_counts_only_omits_issues(tmp_path):
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_basic_style_2.py": """import sys
print("WARNING: foo at a.txt Line number: 1")
sys.exit(1)
""",
        },
    )
    out = lint_tool(tmp_path, checks=["basic_style_2"], files=["a.txt"], counts_only=True)
    assert out["ok"] is True
    assert "issues" not in out
    assert out["counts"]["warning"] == 1


def test_lint_limit_truncates(tmp_path):
    body = "\n".join(
        f'print("WARNING: msg{i} at a.txt Line number: {i}")' for i in range(1, 21)
    )
    _seed_all_scripts(
        tmp_path,
        {
            "tools/linting/check_basic_style_2.py": f"import sys\n{body}\nsys.exit(1)\n",
        },
    )
    out = lint_tool(
        tmp_path,
        checks=["basic_style_2"],
        files=["a.txt"],
        limit=5,
    )
    assert out["issues_total_after_filter"] == 20
    assert len(out["issues"]) == 5
    assert out["truncated"] is True


def test_lint_per_check_failure_isolated(tmp_path):
    """One missing script doesn't bring down the rest of the run."""
    _seed_all_scripts(tmp_path, {})
    (tmp_path / "tools" / "linting" / "check_braces.py").unlink()
    out = lint_tool(tmp_path, checks=["braces", "basic_style"], mode="all")
    assert out["ok"] is True  # overall pass
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["braces"]["ok"] is False
    assert by_name["basic_style"]["ok"] is True
