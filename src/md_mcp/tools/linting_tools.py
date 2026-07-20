"""Linting & branch-review tools.

The MCP surface exposes a single **`lint`** tool that runs the whole linting
suite. The functions in this module are the per-check wrappers around scripts
in `Millennium-Dawn/tools/`:

  * `tools/linting/check_common_mistakes.py`        — `file:line: message`
  * `tools/linting/validate_mod_encoding.py`        — per-file `Valid` / `Invalid UTF-8 encoding`
  * `tools/linting/validate_localization_encoding.py` — per-file `Missing UTF-8 BOM`
  * `tools/analysis/review_branch.py`               — freeform diff summary

Brace, style, and coding-standards checks were absorbed into
`tools/validation/validate_style.py` on the mod side; reach them through the
`validators=` path (`style`) rather than a per-check wrapper here.

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
from ..validators import SLOW_VALIDATORS, ValidatorRunner
from .lint_validators import run_validators_for_lint, select_validators

_LINT_LINE_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):\s*(?P<msg>.+)$")

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
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        if isinstance(e, subprocess.TimeoutExpired):
            return {"ok": False, "error": "check_common_mistakes.py timed out after 300s"}
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
        try:
            line_no = int(m.group("line"))
        except ValueError:
            continue
        issues.append(
            {
                "file": file_path,
                "line": line_no,
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
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        if isinstance(e, subprocess.TimeoutExpired):
            return {"ok": False, "error": "review_branch.py timed out after 120s"}
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
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        if isinstance(e, subprocess.TimeoutExpired):
            return None, f"{script.name} timed out after {timeout}s"
        return None, f"Subprocess failed: {e}"
    return proc, None


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


# ---------------------------------------------------------------------------
# Unified lint dispatcher
# ---------------------------------------------------------------------------

_VALID_MODES: tuple[str, ...] = ("changed", "staged", "all")

_ALL_CHECKS: tuple[str, ...] = (
    "common_mistakes",
    "mod_encoding",
    "loc_encoding",
)


def lint_tool(
    mod_root: Path,
    *,
    mode: str = "changed",
    files: Optional[List[str]] = None,
    checks: Optional[Sequence[str]] = None,
    validators: Optional[Sequence[str]] = None,
    severity_min: str = "info",
    limit: int = 500,
    counts_only: bool = False,
    validator_runner: Optional[ValidatorRunner] = None,
) -> dict:
    """Run the linting suite. Aggregates issues from every checker into one response.

    Args:
        mode          — `changed` (default) | `staged` | `all`.
                        `changed` = staged + unstaged + untracked (everything `git status` sees).
                        `staged`  = only files in the git index.
                        `all`     = brute-scan every matching file under mod_root.
                        Ignored when `files=` is given.
        files         — explicit mod-relative paths. Each checker filters by its
                        own file pattern (e.g. braces ignores `.yml`).
        checks        — subset of `_ALL_CHECKS` to run; omit for all.
        validators    — also run mod validators, merged into the same output.
                        `["auto"]` selects by the domain of the files in scope,
                        `["*"]` runs every fast validator, explicit names run
                        exactly those; sentinels and names union. Slower than
                        the lint scripts — validators scan their whole domain.
        severity_min  — `info` | `warning` | `error`. Drops issues below floor.
        limit         — cap returned issues. `counts_only=True` skips the array.
        counts_only   — return only per-check counts; no `issues` array.
        validator_runner — injected shared runner; constructed lazily if omitted.
    """
    if files is None and mode not in _VALID_MODES:
        return {
            "ok": False,
            "error": f"Invalid mode '{mode}'. Use one of: {list(_VALID_MODES)}",
        }

    selected = list(checks) if checks else list(_ALL_CHECKS)
    unknown = [c for c in selected if c not in _ALL_CHECKS]
    if unknown:
        return {
            "ok": False,
            "error": f"Unknown check(s): {unknown}. Valid: {list(_ALL_CHECKS)}",
        }

    # Resolve the canonical "files of interest" set.
    #   relevant=None means "no filter — let each script do its native --mode all"
    #   relevant=[]   means "user has nothing in scope — every check no-ops"
    if files is not None:
        relevant: Optional[List[str]] = list(files)
    elif mode == "all":
        relevant = None
    elif mode == "changed":
        relevant = _changed_files(mod_root)
    else:  # staged
        relevant = _staged_files(mod_root)

    relevant_set: Optional[set] = set(relevant) if relevant is not None else None

    # Expand the validators request up front so bad names fail fast.
    validator_names: List[str] = []
    runner: Optional[ValidatorRunner] = None
    if validators:
        runner = validator_runner or ValidatorRunner(mod_root)
        available = {v.name for v in runner.list()}
        expanded: set = set()
        for v in validators:
            if v == "*":
                expanded |= available - SLOW_VALIDATORS
            elif v != "auto":
                expanded.add(v)
        unknown_validators = sorted(expanded - available)
        if unknown_validators:
            return {
                "ok": False,
                "error": (
                    f"Unknown validator(s): {unknown_validators}. "
                    f"Valid: {sorted(available)} plus 'auto' and '*'"
                ),
            }
        if "auto" in validators:
            expanded |= set(select_validators(relevant, available))
        validator_names = sorted(expanded)

    if relevant is not None:
        mod_files: Optional[List[str]] = [f for f in relevant if f.endswith(".mod")]
        loc_files: Optional[List[str]] = [
            f for f in relevant if f.startswith("localisation/english/") and f.endswith(".yml")
        ]
    else:
        # mode=all: mod_encoding + loc_encoding auto-discover when files=None.
        mod_files = None
        loc_files = None

    def _skipped() -> dict:
        return {
            "ok": True,
            "total": 0,
            "issues": [],
            "exit_code": 0,
            "skipped": "no files in scope",
        }

    def _maybe(files_list: Optional[List[str]], runner: Callable[[], dict]) -> dict:
        if files_list is not None and not files_list:
            return _skipped()
        return runner()

    runners: Dict[str, Callable[[], dict]] = {
        "common_mistakes": lambda: lint_common_mistakes_tool(
            mod_root,
            mode="all" if relevant is None else "staged",
            files=relevant,
        ),
        "mod_encoding": lambda: _maybe(
            mod_files, lambda: lint_mod_encoding_tool(mod_root, files=mod_files)
        ),
        "loc_encoding": lambda: _maybe(
            loc_files, lambda: lint_loc_encoding_tool(mod_root, files=loc_files)
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
        if result.get("skipped"):
            check_summary["skipped"] = result["skipped"]
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

    if validator_names and runner is not None:
        v_entries, v_issues = run_validators_for_lint(
            runner,
            validator_names,
            staged_only=(mode == "staged" and files is None),
            relevant_set=relevant_set,
        )
        per_check.extend(v_entries)
        for i in v_issues:
            sev = i.get("severity", "info")
            overall[sev] = overall.get(sev, 0) + 1
        all_issues.extend(v_issues)

    floor = _SEVERITY_RANK.get(severity_min, 0)
    filtered = [i for i in all_issues if _SEVERITY_RANK.get(i.get("severity", "info"), 0) >= floor]
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
    if validators:
        summary["validators_run"] = validator_names
    if not counts_only:
        summary["issues"] = issues_capped

    return enforce_budget(summary, heavy_keys=("issues",))


def _staged_files(mod_root: Path) -> List[str]:
    """Files in the git index (staged for commit)."""
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
    return [line for line in proc.stdout.splitlines() if line]


def _changed_files(mod_root: Path) -> List[str]:
    """Every file `git status` reports — staged, unstaged, and untracked.

    Parses `git status --porcelain` (stable machine-readable format).
    `--untracked-files=all` lists files inside untracked directories
    individually; without it git collapses them to `?? dir/` and the files
    never reach the per-check filters.
    For renames (`R  old -> new`) the **new** path is returned.
    Deletions are skipped — there's nothing to lint for a removed file.
    Returns [] when `mod_root` isn't a git repo.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
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

    files: List[str] = []
    seen: set = set()
    for raw in proc.stdout.splitlines():
        if len(raw) < 4:
            continue
        status = raw[:2]
        path = raw[3:]
        # Renames: "R  old -> new" — take the destination.
        if "R" in status and " -> " in path:
            path = path.split(" -> ", 1)[1]
        # Skip deletions; nothing to lint.
        if status.strip() == "D":
            continue
        # Strip surrounding quotes that git uses for paths with special chars.
        path = path.strip().strip('"')
        if path and path not in seen:
            seen.add(path)
            files.append(path)
    return files
