"""Tests for the linting wrapper tools.

Each wrapper subprocesses a `Millennium-Dawn/tools/linting/*.py` script and
regex-parses its output. We don't depend on the real mod scripts here — we
write tiny stand-ins at the expected path that emit the same line format,
then assert the parser produces the right structured issues.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from md_mcp.tools.linting_tools import lint_mod_encoding_tool


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
        (lint_mod_encoding_tool, ("mod_root", "files", "limit")),
    ]:
        params = inspect.signature(fn).parameters
        for p in required:
            assert p in params, f"{fn.__name__} missing param: {p}"


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


def test_lint_mod_encoding_missing_script(tmp_path):
    (tmp_path / "descriptor.mod").write_text('name = "x"\n', encoding="utf-8")
    out = lint_mod_encoding_tool(tmp_path)
    assert out["ok"] is False
    assert "validate_mod_encoding.py" in out["error"]


def test_lint_mod_encoding_no_files_skipped(tmp_path):
    """No .mod files found → skipped rather than running the script with empty args."""
    _make_script(
        tmp_path,
        "tools/linting/validate_mod_encoding.py",
        "import sys\nsys.exit(0)\n",
    )
    out = lint_mod_encoding_tool(tmp_path)
    assert out["ok"] is True
    assert out["total"] == 0
    assert out["skipped"] == "no .mod files found"
    assert out["issues"] == []


def test_lint_mod_encoding_explicit_empty_files_skipped(tmp_path):
    """files=[] is an empty scope, same as discovering nothing."""
    _make_script(
        tmp_path,
        "tools/linting/validate_mod_encoding.py",
        "import sys\nprint('should not run')\nsys.exit(1)\n",
    )
    out = lint_mod_encoding_tool(tmp_path, files=[])
    assert out["ok"] is True
    assert out["total"] == 0
    assert out["skipped"] == "no .mod files found"


# ---------------------------------------------------------------------------
# Budget guard
# ---------------------------------------------------------------------------


def test_lint_mod_encoding_caps_at_limit(tmp_path):
    # Generate 50 stub issues, ask for limit=10.
    (tmp_path / "descriptor.mod").write_text('name = "x"\n', encoding="utf-8")
    issues_block = "\n".join(
        f"f{i}.mod: Invalid UTF-8 encoding - byte 0x80 at position {i}" for i in range(1, 51)
    )
    _make_script(
        tmp_path,
        "tools/linting/validate_mod_encoding.py",
        f"""import sys
print({issues_block!r}, file=sys.stderr)
sys.exit(1)
""",
    )
    out = lint_mod_encoding_tool(tmp_path, limit=10)
    assert out["ok"] is True
    assert out["total"] == 50
    assert out["returned"] == 10
    assert out["truncated"] is True
    assert len(out["issues"]) == 10
