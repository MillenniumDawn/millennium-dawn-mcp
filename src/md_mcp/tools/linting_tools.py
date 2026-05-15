"""Linting & branch-review tools.

These wrap two scripts in `Millennium-Dawn/tools/` that aren't validator-shaped:
  * `tools/linting/check_common_mistakes.py` — text-line output `file:line: message`
  * `tools/analysis/review_branch.py`         — freeform diff summary
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

_LINT_LINE_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):\s*(?P<msg>.+)$")


def lint_common_mistakes_tool(
    mod_root: Path,
    *,
    mode: str = "staged",
    files: Optional[List[str]] = None,
) -> dict:
    """Run `tools/linting/check_common_mistakes.py` and return structured issues.

    Args:
        mode  — `staged` (only git-staged files; fast) or `all` (full scan)
        files — explicit file list (mod-relative); overrides `mode`
    """
    script = mod_root / "tools" / "linting" / "check_common_mistakes.py"
    if not script.exists():
        return {"ok": False, "error": f"check_common_mistakes.py not found at {script}"}

    cmd = [sys.executable, str(script)]
    if files:
        cmd.extend(files)
    else:
        cmd.extend(["--mode", mode])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(mod_root),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "check_common_mistakes.py timed out after 300s"}
    except Exception as e:
        return {"ok": False, "error": f"Subprocess failed: {e}"}

    issues: List[dict] = []
    for line in proc.stdout.splitlines():
        m = _LINT_LINE_RE.match(line.strip())
        if not m:
            continue
        # The script also prints summary lines like `Checked N files` — skip if file
        # doesn't look like a path (no slash).
        file_path = m.group("file")
        if "/" not in file_path and "\\" not in file_path:
            continue
        issues.append(
            {
                "file": file_path,
                "line": int(m.group("line")),
                "message": m.group("msg"),
                "severity": "warning",
            }
        )

    return {
        "ok": True,
        "issues": issues,
        "count": len(issues),
        "mode": "files" if files else mode,
        "exit_code": proc.returncode,
        "stderr": proc.stderr[-2000:] if proc.stderr else "",
    }


def review_branch_tool(mod_root: Path, base: str = "main") -> dict:
    """Run `tools/analysis/review_branch.py` and return its raw text summary.

    The script produces a human-readable digest (commits, file diffs, content
    summary). We return it as a single text blob — the agent can quote it or extract
    the parts it needs.
    """
    script = mod_root / "tools" / "analysis" / "review_branch.py"
    if not script.exists():
        return {"ok": False, "error": f"review_branch.py not found at {script}"}

    cmd = [sys.executable, str(script), base]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(mod_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "review_branch.py timed out after 120s"}
    except Exception as e:
        return {"ok": False, "error": f"Subprocess failed: {e}"}

    return {
        "ok": True,
        "base": base,
        "report": proc.stdout,
        "exit_code": proc.returncode,
        "stderr": proc.stderr[-2000:] if proc.stderr else "",
    }
