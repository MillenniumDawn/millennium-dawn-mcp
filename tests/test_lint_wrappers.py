"""Tests for the linting wrapper tools.

Each wrapper subprocesses a `Millennium-Dawn/tools/linting/*.py` script and
regex-parses its output. We don't depend on the real mod scripts here — we
write tiny stand-ins at the expected path that emit the same line format,
then assert the parser produces the right structured issues.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from md_mcp.tools.linting_tools import (
    lint_basic_style_tool,
    lint_braces_tool,
    lint_mod_encoding_tool,
)


def _make_script(root: Path, rel: str, body: str) -> Path:
    """Write an executable Python script at root/rel. Returns the path."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


# ---------------------------------------------------------------------------
# Signature guards
# ---------------------------------------------------------------------------


def test_signatures_lock_api():
    """If someone reshuffles the params, this fires before runtime callers do."""
    for fn, required in [
        (lint_braces_tool, ("mod_root", "files", "limit")),
        (lint_basic_style_tool, ("mod_root", "mode", "files", "limit")),
        (lint_mod_encoding_tool, ("mod_root", "files", "limit")),
    ]:
        params = inspect.signature(fn).parameters
        for p in required:
            assert p in params, f"{fn.__name__} missing param: {p}"


# ---------------------------------------------------------------------------
# lint_braces
# ---------------------------------------------------------------------------


def test_lint_braces_requires_files(tmp_path):
    out = lint_braces_tool(tmp_path, files=[])
    assert out["ok"] is False
    assert "files" in out["error"]


def test_lint_braces_missing_script(tmp_path):
    out = lint_braces_tool(tmp_path, files=["x.txt"])
    assert out["ok"] is False
    assert "check_braces.py" in out["error"]


def test_lint_braces_parses_header_and_issues(tmp_path):
    _make_script(
        tmp_path,
        "tools/linting/check_braces.py",
        """import sys
print(file=sys.stderr)
print("common/national_focus/foo.txt:", file=sys.stderr)
print("  Line 12, Column 5: Opening brace '{' without matching closing brace", file=sys.stderr)
print("  Line 47, Column 1: Closing brace '}' without matching opening brace", file=sys.stderr)
print("\\n[X] Brace validation failed! Please fix the issues above.", file=sys.stderr)
sys.exit(1)
""",
    )

    out = lint_braces_tool(tmp_path, files=["common/national_focus/foo.txt"])
    assert out["ok"] is True
    assert out["total"] == 2
    assert out["exit_code"] == 1
    assert out["issues"][0] == {
        "file": "common/national_focus/foo.txt",
        "line": 12,
        "col": 5,
        "message": "Opening brace '{' without matching closing brace",
        "severity": "error",
    }
    assert out["issues"][1]["line"] == 47


def test_lint_braces_clean_run(tmp_path):
    _make_script(
        tmp_path,
        "tools/linting/check_braces.py",
        "import sys\nsys.exit(0)\n",
    )
    out = lint_braces_tool(tmp_path, files=["foo.txt"])
    assert out["ok"] is True
    assert out["total"] == 0
    assert out["exit_code"] == 0


# ---------------------------------------------------------------------------
# lint_basic_style
# ---------------------------------------------------------------------------


def test_lint_basic_style_parses_both_error_shapes(tmp_path):
    _make_script(
        tmp_path,
        "tools/linting/check_basic_style.py",
        """import sys
print("  [90m[timer] file collection: 0.001s[0m")
print("Validating Basic Style (Mode: all)")
print("ERROR: Possible missing curly brace '}' detected at /tmp/x.txt Line number: 4")
print("ERROR: A possible missing round bracket ( or ) in file /tmp/y.txt ( = 2 ) = 1")
print("Errors detected: 2")
sys.exit(1)
""",
    )
    out = lint_basic_style_tool(tmp_path, mode="all")
    assert out["ok"] is True
    assert out["total"] == 2
    assert out["mode"] == "all"
    assert any(
        "Line number" not in i.get("message", "") and i.get("line") == 4 for i in out["issues"]
    )
    assert any("(" in i["message"] for i in out["issues"])


def test_lint_basic_style_files_overrides_mode(tmp_path):
    """When `files` is provided, mode flag is replaced with raw file list.

    The stub script emits a parseable ERROR line containing the first arg it
    received so the parser captures it as a structured issue we can inspect.
    """
    _make_script(
        tmp_path,
        "tools/linting/check_basic_style.py",
        """import sys
first = sys.argv[1] if len(sys.argv) > 1 else "(none)"
print(f"ERROR: saw arg at {first} Line number: 1")
sys.exit(0)
""",
    )
    out = lint_basic_style_tool(tmp_path, files=["a.txt", "b.txt"])
    assert out["ok"] is True
    assert out["mode"] == "files"
    assert out["total"] == 1
    assert out["issues"][0]["file"] == "a.txt"


# ---------------------------------------------------------------------------
# lint_mod_encoding
# ---------------------------------------------------------------------------


def test_lint_mod_encoding_auto_discovers_mod_files(tmp_path):
    (tmp_path / "descriptor.mod").write_text('name = "x"\n', encoding="utf-8")
    _make_script(
        tmp_path,
        "tools/linting/validate_mod_encoding.py",
        """import sys
for arg in sys.argv[1:]:
    print(f"{arg}: Valid UTF-8 encoding")
sys.exit(0)
""",
    )
    out = lint_mod_encoding_tool(tmp_path)
    assert out["ok"] is True
    assert out["checked"] >= 1
    assert out["total"] == 0  # no failures


def test_lint_mod_encoding_reports_invalid(tmp_path):
    (tmp_path / "descriptor.mod").write_text('name = "x"\n', encoding="utf-8")
    _make_script(
        tmp_path,
        "tools/linting/validate_mod_encoding.py",
        """import sys
print("descriptor.mod: Invalid UTF-8 encoding - byte 0x80 at position 12", file=sys.stderr)
sys.exit(1)
""",
    )
    out = lint_mod_encoding_tool(tmp_path)
    assert out["ok"] is True
    assert out["total"] == 1
    issue = out["issues"][0]
    assert issue["file"] == "descriptor.mod"
    assert "Invalid UTF-8 encoding" in issue["message"]
    assert issue["severity"] == "error"


def test_lint_mod_encoding_no_files_error(tmp_path):
    """No .mod files found → ok=False rather than running the script with empty args."""
    _make_script(
        tmp_path,
        "tools/linting/validate_mod_encoding.py",
        "import sys\nsys.exit(0)\n",
    )
    out = lint_mod_encoding_tool(tmp_path)
    assert out["ok"] is False
    assert ".mod" in out["error"]


# ---------------------------------------------------------------------------
# Budget guard
# ---------------------------------------------------------------------------


def test_lint_braces_caps_at_limit(tmp_path):
    # Generate 50 stub issues, ask for limit=10.
    issues_block = "\n".join(
        f"  Line {i}, Column 1: Opening brace '{{' without matching closing brace"
        for i in range(1, 51)
    )
    _make_script(
        tmp_path,
        "tools/linting/check_braces.py",
        f"""import sys
print("foo.txt:", file=sys.stderr)
print({issues_block!r}, file=sys.stderr)
sys.exit(1)
""",
    )
    out = lint_braces_tool(tmp_path, files=["foo.txt"], limit=10)
    assert out["ok"] is True
    assert out["total"] == 50
    assert out["returned"] == 10
    assert out["truncated"] is True
    assert len(out["issues"]) == 10
