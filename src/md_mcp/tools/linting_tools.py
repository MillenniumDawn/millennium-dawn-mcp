"""Linting & branch-review tools.

The MCP surface exposes a single **`lint`** tool that runs the whole linting
suite. The functions in this module are the per-check wrappers around scripts
in `Millennium-Dawn/tools/`:

  * `tools/linting/check_common_mistakes.py`        — `file:line: message`
  * `tools/linting/check_braces.py`                 — `<file>:` header + indented issues
  * `tools/linting/check_basic_style.py`            — `ERROR: ... at <file> Line number: N`
  * `tools/linting/check_basic_style_2.py`          — `WARNING: ... at <file> Line number: N`
  * `tools/linting/coding_standards.py`             — `WARNING: ... in <file> Line number: N`
  * `tools/linting/validate_mod_encoding.py`        — per-file `Valid` / `Invalid UTF-8 encoding`
  * `tools/linting/validate_localization_encoding.py` — per-file `Missing UTF-8 BOM`
  * `tools/analysis/review_branch.py`               — freeform diff summary

Pattern: subprocess the script, regex-parse its line output, emit structured
issues. Each wrapper returns `{name, ok, issues, exit_code, stderr_tail, counts}`.
The orchestrator (`lint_tool`) aggregates these, applies severity + cap filters,
and wraps the result in `enforce_budget`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from ..util.response import enforce_budget

_LINT_LINE_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):\s*(?P<msg>.+)$")

# `<file>:` header line followed by indented `Line N, Column N: msg` lines.
_BRACE_HEADER_RE = re.compile(r"^(?P<file>[^\s].+):$")
_BRACE_ISSUE_RE = re.compile(
    r"^\s*Line\s+(?P<line>\d+),\s+Column\s+(?P<col>\d+):\s*(?P<msg>.+)$"
)

# Unified style/standards format: ERROR|WARNING + at|in + file + Line number: N.
# Covers check_basic_style.py, check_basic_style_2.py, coding_standards.py.
_STYLE_LINE_RE = re.compile(
    r"^(?P<sev>ERROR|WARNING):\s+(?P<msg>.+?)\s+(?:at|in)\s+(?P<file>.+?)"
    r"\s+Line number:\s+(?P<line>\d+)\s*$"
)
_STYLE_BARE_FILE_RE = re.compile(
    r"^(?P<sev>ERROR|WARNING):\s+(?P<msg>.+?)\s+in file\s+(?P<file>.+?)\s+(?P<detail>\(\s*=.*)$"
)

# validate_mod_encoding emits one line per file on stdout/stderr.
_MOD_ENC_OK_RE = re.compile(r"^(?P<file>.+?):\s+Valid UTF-8 encoding\s*$")
_MOD_ENC_BAD_RE = re.compile(r"^(?P<file>.+?):\s+Invalid UTF-8 encoding\s+-\s+(?P<msg>.+)$")

# validate_localization_encoding emits the BOM-missing diagnostic.
_LOC_ENC_BAD_RE = re.compile(r"^(?P<file>.+?):\s+Missing UTF-8 BOM.*$")

# ANSI escape stripper for scripts that emit colour codes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


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


def _run_script(
    script: Path,
    mod_root: Path,
    args: List[str],
    *,
    timeout: int = 120,
) -> "tuple[Optional[subprocess.CompletedProcess], Optional[str]]":
    """Subprocess a tools/ script. Returns (proc, error_msg). One of them is None."""
    if not script.exists():
        return None, f"{script.name} not found at {script}"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), *args],
            cwd=str(mod_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"{script.name} timed out after {timeout}s"
    except Exception as e:
        return None, f"Subprocess failed: {e}"
    return proc, None


def lint_braces_tool(
    mod_root: Path,
    *,
    files: List[str],
    limit: int = 200,
) -> dict:
    """Run `tools/linting/check_braces.py` against an explicit file list.

    The script requires file arguments — there's no auto-discovery mode. The
    output groups issues under `<file>:` headers with indented
    `Line N, Column N: msg` entries; we re-attach the header file to each issue.
    """
    if not files:
        return {
            "ok": False,
            "error": "lint_braces requires files=[...]. The script has no auto-discovery mode.",
        }
    script = mod_root / "tools" / "linting" / "check_braces.py"
    proc, err = _run_script(script, mod_root, files)
    if proc is None:
        return {"ok": False, "error": err}

    issues: List[dict] = []
    current_file: Optional[str] = None
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for raw in combined.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip()
        if not line.strip():
            continue
        if line.startswith("Error: File not found"):
            issues.append({"file": line.split(":", 2)[-1].strip(), "message": "File not found", "severity": "error"})
            continue
        if line.startswith("❌") or line.startswith("Usage:"):
            continue
        m = _BRACE_HEADER_RE.match(line)
        if m and not line.startswith("  "):
            current_file = m.group("file").strip()
            continue
        m = _BRACE_ISSUE_RE.match(line)
        if m:
            issues.append(
                {
                    "file": current_file or "<unknown>",
                    "line": int(m.group("line")),
                    "col": int(m.group("col")),
                    "message": m.group("msg").strip(),
                    "severity": "error",
                }
            )

    truncated = len(issues) > limit
    return enforce_budget(
        {
            "ok": True,
            "total": len(issues),
            "returned": min(limit, len(issues)),
            "truncated": truncated,
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-1000:],
            "issues": issues[:limit],
        },
        heavy_keys=("issues",),
    )


def lint_basic_style_tool(
    mod_root: Path,
    *,
    mode: str = "staged",
    files: Optional[List[str]] = None,
    limit: int = 200,
) -> dict:
    """Run `tools/linting/check_basic_style.py` (bracket / paren balance).

    Args:
        mode  — `staged` (only git-staged files; fast) or `all` (full scan)
        files — explicit file list; takes precedence over `mode`
    """
    script = mod_root / "tools" / "linting" / "check_basic_style.py"
    args: List[str] = list(files) if files else ["--mode", mode]
    proc, err = _run_script(script, mod_root, args)
    if proc is None:
        return {"ok": False, "error": err}

    issues: List[dict] = []
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for raw in combined.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip()
        if not line.strip() or "[timer]" in line:
            continue
        m = _STYLE_LINE_RE.match(line)
        if m:
            issues.append(
                {
                    "file": m.group("file").strip(),
                    "line": int(m.group("line")),
                    "message": m.group("msg").strip(),
                    "severity": "error" if m.group("sev") == "ERROR" else "warning",
                }
            )
            continue
        m = _STYLE_BARE_FILE_RE.match(line)
        if m:
            issues.append(
                {
                    "file": m.group("file").strip(),
                    "message": f"{m.group('msg').strip()} {m.group('detail').strip()}",
                    "severity": "error" if m.group("sev") == "ERROR" else "warning",
                }
            )

    truncated = len(issues) > limit
    return enforce_budget(
        {
            "ok": True,
            "mode": "files" if files else mode,
            "total": len(issues),
            "returned": min(limit, len(issues)),
            "truncated": truncated,
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-1000:],
            "issues": issues[:limit],
        },
        heavy_keys=("issues",),
    )


def lint_mod_encoding_tool(
    mod_root: Path,
    *,
    files: Optional[List[str]] = None,
    limit: int = 200,
) -> dict:
    """Run `tools/linting/validate_mod_encoding.py` against `.mod` files.

    With no `files`, defaults to every `.mod` file under the mod root.
    """
    script = mod_root / "tools" / "linting" / "validate_mod_encoding.py"
    if files is None:
        files = [str(p.relative_to(mod_root)) for p in mod_root.glob("*.mod") if p.is_file()]
        if not files:
            return {"ok": False, "error": "No .mod files found under mod root"}
    if not files:
        return {"ok": False, "error": "lint_mod_encoding requires at least one .mod file"}

    proc, err = _run_script(script, mod_root, list(files))
    if proc is None:
        return {"ok": False, "error": err}

    issues: List[dict] = []
    checked: List[str] = []
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for raw in combined.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip()
        if not line.strip():
            continue
        m = _MOD_ENC_OK_RE.match(line)
        if m:
            checked.append(m.group("file").strip())
            continue
        m = _MOD_ENC_BAD_RE.match(line)
        if m:
            issues.append(
                {
                    "file": m.group("file").strip(),
                    "message": f"Invalid UTF-8 encoding: {m.group('msg').strip()}",
                    "severity": "error",
                }
            )

    truncated = len(issues) > limit
    return enforce_budget(
        {
            "ok": True,
            "checked": len(checked) + len(issues),
            "total": len(issues),
            "returned": min(limit, len(issues)),
            "truncated": truncated,
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-1000:],
            "issues": issues[:limit],
        },
        heavy_keys=("issues",),
    )


def lint_basic_style_2_tool(
    mod_root: Path,
    *,
    mode: str = "staged",
    files: Optional[List[str]] = None,
    limit: int = 200,
) -> dict:
    """Run `tools/linting/check_basic_style_2.py` (secondary style: missing spaces around braces, etc.).

    Emits `WARNING:` lines with the same `at <file> Line number: N` shape as
    `check_basic_style.py`. Manual-stage in pre-commit but cheap enough to
    include in the unified lint pass.
    """
    script = mod_root / "tools" / "linting" / "check_basic_style_2.py"
    args: List[str] = list(files) if files else ["--mode", mode]
    proc, err = _run_script(script, mod_root, args)
    if proc is None:
        return {"ok": False, "error": err}

    issues = _parse_style_lines((proc.stdout or "") + "\n" + (proc.stderr or ""))
    truncated = len(issues) > limit
    return enforce_budget(
        {
            "ok": True,
            "mode": "files" if files else mode,
            "total": len(issues),
            "returned": min(limit, len(issues)),
            "truncated": truncated,
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-1000:],
            "issues": issues[:limit],
        },
        heavy_keys=("issues",),
    )


def lint_coding_standards_tool(
    mod_root: Path,
    *,
    mode: str = "staged",
    limit: int = 200,
) -> dict:
    """Run `tools/linting/coding_standards.py` (focus-ID format, news_event format, etc.).

    The script only accepts `--mode {staged,all}` — no per-file invocation,
    so `files=` isn't supported here. Emits `WARNING: ... in <file> Line
    number: N`.
    """
    script = mod_root / "tools" / "linting" / "coding_standards.py"
    proc, err = _run_script(script, mod_root, ["--mode", mode])
    if proc is None:
        return {"ok": False, "error": err}

    issues = _parse_style_lines((proc.stdout or "") + "\n" + (proc.stderr or ""))
    truncated = len(issues) > limit
    return enforce_budget(
        {
            "ok": True,
            "mode": mode,
            "total": len(issues),
            "returned": min(limit, len(issues)),
            "truncated": truncated,
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-1000:],
            "issues": issues[:limit],
        },
        heavy_keys=("issues",),
    )


def lint_loc_encoding_tool(
    mod_root: Path,
    *,
    files: Optional[List[str]] = None,
    limit: int = 200,
) -> dict:
    """Run `tools/linting/validate_localization_encoding.py` (English loc YAML BOM check).

    With no `files`, the script auto-discovers every English loc YAML.
    `--fix` is **never** passed — this tool is read-only by design; use
    Edit/Write to add BOMs.
    """
    script = mod_root / "tools" / "linting" / "validate_localization_encoding.py"
    args: List[str] = list(files) if files else []
    proc, err = _run_script(script, mod_root, args)
    if proc is None:
        return {"ok": False, "error": err}

    issues: List[dict] = []
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for raw in combined.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip()
        if not line.strip():
            continue
        m = _LOC_ENC_BAD_RE.match(line)
        if m:
            issues.append(
                {
                    "file": m.group("file").strip(),
                    "message": "Missing UTF-8 BOM (required for HOI4 localization)",
                    "severity": "error",
                }
            )

    truncated = len(issues) > limit
    return enforce_budget(
        {
            "ok": True,
            "total": len(issues),
            "returned": min(limit, len(issues)),
            "truncated": truncated,
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-1000:],
            "issues": issues[:limit],
        },
        heavy_keys=("issues",),
    )


def _parse_style_lines(text: str) -> List[dict]:
    """Shared parser for the ERROR|WARNING + at|in style format."""
    issues: List[dict] = []
    for raw in text.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip()
        if not line.strip() or "[timer]" in line:
            continue
        m = _STYLE_LINE_RE.match(line)
        if m:
            issues.append(
                {
                    "file": m.group("file").strip(),
                    "line": int(m.group("line")),
                    "message": m.group("msg").strip(),
                    "severity": "error" if m.group("sev") == "ERROR" else "warning",
                }
            )
            continue
        m = _STYLE_BARE_FILE_RE.match(line)
        if m:
            issues.append(
                {
                    "file": m.group("file").strip(),
                    "message": f"{m.group('msg').strip()} {m.group('detail').strip()}",
                    "severity": "error" if m.group("sev") == "ERROR" else "warning",
                }
            )
    return issues


# ---------------------------------------------------------------------------
# Unified lint dispatcher
# ---------------------------------------------------------------------------

# Pre-commit excludes these from .txt-pattern hooks (matches the patterns in
# Millennium-Dawn/.pre-commit-config.yaml line ~126).
_TXT_LINT_EXCLUDES_RE = re.compile(r"(?:^|/)(Changelog\.txt|AUTHORS\.txt|descriptions[^/]*\.txt)$")


_ALL_CHECKS: tuple[str, ...] = (
    "common_mistakes",
    "braces",
    "basic_style",
    "basic_style_2",
    "coding_standards",
    "mod_encoding",
    "loc_encoding",
)


def lint_tool(
    mod_root: Path,
    *,
    mode: str = "staged",
    files: Optional[List[str]] = None,
    checks: Optional[Sequence[str]] = None,
    severity_min: str = "info",
    limit: int = 500,
    counts_only: bool = False,
) -> dict:
    """Run the linting suite. Aggregates issues from every checker into one response.

    Args:
        mode          — `staged` (default) or `all`. Ignored when `files=` given.
        files         — explicit mod-relative paths. Each checker selects the
                        files matching its file pattern.
        checks        — subset of `_ALL_CHECKS` to run; omit for all.
        severity_min  — `info` | `warning` | `error`. Drops issues below floor.
        limit         — cap returned issues. `counts_only=True` skips the array.
        counts_only   — return only per-check counts; no `issues` array.
    """
    selected = list(checks) if checks else list(_ALL_CHECKS)
    unknown = [c for c in selected if c not in _ALL_CHECKS]
    if unknown:
        return {
            "ok": False,
            "error": f"Unknown check(s): {unknown}. Valid: {list(_ALL_CHECKS)}",
        }

    txt_files = _select_files(mod_root, mode, files, _is_lintable_txt) if (
        "braces" in selected
    ) else []
    mod_files = _select_files(mod_root, mode, files, lambda p: p.endswith(".mod")) if (
        "mod_encoding" in selected
    ) else []
    loc_files = _select_files(
        mod_root, mode, files, lambda p: p.startswith("localisation/english/") and p.endswith(".yml")
    ) if "loc_encoding" in selected else []

    runners: Dict[str, Callable[[], dict]] = {
        "common_mistakes": lambda: lint_common_mistakes_tool(
            mod_root, mode=mode, files=files
        ),
        "braces": lambda: lint_braces_tool(mod_root, files=txt_files),
        "basic_style": lambda: lint_basic_style_tool(
            mod_root, mode=mode, files=files
        ),
        "basic_style_2": lambda: lint_basic_style_2_tool(
            mod_root, mode=mode, files=files
        ),
        "coding_standards": lambda: lint_coding_standards_tool(mod_root, mode=mode),
        "mod_encoding": lambda: lint_mod_encoding_tool(mod_root, files=mod_files or None),
        "loc_encoding": lambda: lint_loc_encoding_tool(
            mod_root, files=loc_files or files
        ),
    }

    per_check: List[dict] = []
    all_issues: List[dict] = []
    overall = {"error": 0, "warning": 0, "info": 0}

    for name in selected:
        result = runners[name]()
        check_summary = {
            "name": name,
            "ok": bool(result.get("ok")),
            "total": result.get("total", 0),
            "exit_code": result.get("exit_code"),
        }
        if not result.get("ok"):
            check_summary["error"] = result.get("error")
        else:
            issues = result.get("issues", []) or []
            # Tag each issue with which check produced it (helps the agent).
            for i in issues:
                i.setdefault("check", name)
                sev = i.get("severity", "info")
                overall[sev] = overall.get(sev, 0) + 1
            all_issues.extend(issues)
        per_check.append(check_summary)

    floor = _SEVERITY_RANK.get(severity_min, 0)
    filtered = [
        i for i in all_issues
        if _SEVERITY_RANK.get(i.get("severity", "info"), 0) >= floor
    ]
    truncated = len(filtered) > limit if limit >= 0 else False
    issues_capped = filtered[:limit] if limit >= 0 else filtered

    summary: dict = {
        "ok": True,
        "mode": "files" if files else mode,
        "checks_run": selected,
        "counts": overall,
        "issues_total_after_filter": len(filtered),
        "truncated": truncated,
        "checks": per_check,
    }
    if not counts_only:
        summary["issues"] = issues_capped

    return enforce_budget(summary, heavy_keys=("issues",))


def _is_lintable_txt(path: str) -> bool:
    return path.endswith(".txt") and not _TXT_LINT_EXCLUDES_RE.search(path)


def _select_files(
    mod_root: Path,
    mode: str,
    explicit: Optional[List[str]],
    predicate: Callable[[str], bool],
) -> List[str]:
    """Resolve which files a given check should run against.

    Precedence: explicit `files=` → staged-files diff → all matching files
    under mod_root (slow path, only when `mode=all`).
    """
    if explicit is not None:
        return [f for f in explicit if predicate(f)]
    if mode == "staged":
        try:
            proc = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                cwd=str(mod_root),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception:
            return []
        if proc.returncode != 0:
            return []
        return [f for f in proc.stdout.splitlines() if predicate(f)]
    # mode == "all" — brute scan. Capped at 5000 to avoid pathological cases.
    out: List[str] = []
    for p in mod_root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = str(p.relative_to(mod_root))
        except ValueError:
            continue
        if predicate(rel):
            out.append(rel)
            if len(out) >= 5000:
                break
    return out
