"""Linting & branch-review tools.

The MCP surface exposes a single **`lint`** tool that runs the whole linting
suite. The functions in this module are the per-check wrappers around scripts
in `Millennium-Dawn/tools/`:

  * `tools/linting/check_common_mistakes.py`        — `file:line: message`
  * `tools/linting/validate_mod_encoding.py`        — per-file `Valid` / `Invalid UTF-8 encoding`
  * `tools/linting/validate_localization_encoding.py` — per-file `Missing UTF-8 BOM`
  * `tools/analysis/review_branch.py`               — freeform diff summary

Brace, style, and coding-standards checks were absorbed into
`tools/validation/validate_style.py` on the mod side. The dispatcher runs that
validator by default for full-tree and applicable script scopes rather than
wrapping it as a lint script.

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

from ..util.response import BUDGET_BYTES, enforce_budget
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

# Leave room for status fields and stderr within the response budget.
_MAX_REVIEW_REPORT_BYTES = max(1, BUDGET_BYTES - 12_000)


def _clip_utf8(text: str, max_bytes: int) -> tuple[str, int, int, bool]:
    """Return text clipped at a UTF-8 boundary plus size metadata."""
    raw = text.encode("utf-8")
    total = len(raw)
    if total <= max_bytes:
        return text, total, total, False
    clipped = raw[:max_bytes].decode("utf-8", "ignore")
    return clipped, total, len(clipped.encode("utf-8")), True


def _review_payload(
    base: str,
    *,
    proc: Optional[subprocess.CompletedProcess] = None,
    error: Optional[str] = None,
) -> dict:
    """Shape and guard a review report on both subprocess paths."""
    if proc is None:
        bounded_error, _, _, _ = _clip_utf8(str(error or ""), 2_000)
        result = {
            "ok": False,
            "base": base,
            "error": bounded_error,
            "report": "",
            "report_bytes": 0,
            "report_returned_bytes": 0,
            "report_truncated": False,
            "exit_code": None,
            "stderr": "",
        }
    else:
        raw_report = proc.stdout or ""
        report, report_bytes, returned_bytes, report_truncated = _clip_utf8(
            raw_report, _MAX_REVIEW_REPORT_BYTES
        )
        result = {
            "ok": True,
            "base": base,
            "report": report,
            "report_bytes": report_bytes,
            "report_returned_bytes": returned_bytes,
            "report_truncated": report_truncated,
            "exit_code": proc.returncode,
            "stderr": proc.stderr[-2000:] if proc.stderr else "",
        }
    return enforce_budget(result, heavy_keys=("report", "stderr", "error"))


def _failure_payload(
    script: Path,
    proc: subprocess.CompletedProcess,
    error: str,
) -> dict:
    stderr_tail = (proc.stderr or "")[-1000:]
    output_tail = stderr_tail or (proc.stdout or "")[-1000:]
    if output_tail.strip():
        error = f"{error}. Output tail: {output_tail.strip()}"
    return {
        "ok": False,
        "error": error,
        "exit_code": proc.returncode,
        "stderr_tail": stderr_tail,
    }


def _completed_process_failure(
    script: Path,
    proc: subprocess.CompletedProcess,
    issue_count: int,
) -> Optional[dict]:
    # A Python traceback on stderr means the script died mid-scan: whatever
    # issues were parsed came from a partial run, and exit 1 no longer means
    # "issues found". Report the crash instead of the partial output.
    if "Traceback (most recent call last):" in (proc.stderr or ""):
        return _failure_payload(
            script, proc, f"{script.name} crashed mid-run (traceback on stderr)"
        )

    if proc.returncode == 0 or (proc.returncode == 1 and issue_count):
        return None

    if proc.returncode == 1:
        error = f"{script.name} exited with code 1 without recognized diagnostics"
    else:
        error = f"{script.name} exited with unexpected code {proc.returncode}"
    return _failure_payload(script, proc, error)


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

    def collect(line: str) -> Optional[dict]:
        m = _LINT_LINE_RE.match(line)
        if not m:
            return None
        # The script also prints summary lines like `Checked N files` — skip if file
        # doesn't look like a path (no slash).
        file_path = m.group("file")
        if "/" not in file_path and "\\" not in file_path:
            return None
        try:
            line_no = int(m.group("line"))
        except ValueError:
            return None
        return {
            "file": file_path,
            "line": line_no,
            "message": m.group("msg"),
            "severity": "warning",
        }

    return _run_parsed_script(
        script,
        mod_root,
        list(files) if files else ["--mode", mode],
        collect,
        timeout=300,
        combined=False,
        limit=None,
        extra=lambda proc, issues: {
            "count": len(issues),
            "mode": "files" if files else mode,
            "stderr": proc.stderr[-2000:] if proc.stderr else "",
        },
    )


def review_branch_tool(mod_root: Path, base: str = "main") -> dict:
    """Run `tools/analysis/review_branch.py` and return a bounded text summary.

    The script produces a human-readable digest (commits, file diffs, content
    summary). The report is clipped by UTF-8 bytes, with its original and returned
    sizes exposed so callers can request a narrower review when needed.
    """
    script = mod_root / "tools" / "analysis" / "review_branch.py"
    proc, err = _run_script(script, mod_root, [base])
    if proc is None:
        return _review_payload(base, error=err)
    return _review_payload(base, proc=proc)


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
    except (OSError, ValueError) as e:
        return None, f"Subprocess failed: {e}"
    return proc, None


def _run_parsed_script(
    script: Path,
    mod_root: Path,
    args: List[str],
    collect: Callable[[str], Optional[dict]],
    *,
    timeout: int = 120,
    combined: bool = True,
    limit: Optional[int] = 200,
    extra: Optional[Callable[[subprocess.CompletedProcess, List[dict]], dict]] = None,
) -> dict:
    """Run a script, parse its output, and shape its issue response."""
    proc, err = _run_script(script, mod_root, args, timeout=timeout)
    if proc is None:
        return {"ok": False, "error": err}

    output = (proc.stdout or "") + "\n" + (proc.stderr or "") if combined else proc.stdout or ""
    issues: List[dict] = []
    for raw in output.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip() if combined else raw.strip()
        if not line.strip():
            continue
        issue = collect(line)
        if issue is not None:
            issues.append(issue)

    failure = _completed_process_failure(script, proc, len(issues))
    if failure is not None:
        return failure

    result: dict = {"ok": True, "total": len(issues)}
    if extra is not None:
        result.update(extra(proc, issues))
    result.update({"exit_code": proc.returncode})
    if limit is None:
        result["issues"] = issues
        return result

    result.update(
        {
            "returned": min(limit, len(issues)),
            "truncated": len(issues) > limit,
            "issues": issues[:limit],
        }
    )
    return enforce_budget(result, heavy_keys=("issues",))


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
        return {
            "ok": True,
            "total": 0,
            "issues": [],
            "exit_code": 0,
            "skipped": "no .mod files found",
        }

    checked = 0

    def collect(line: str) -> Optional[dict]:
        nonlocal checked
        m = _MOD_ENC_OK_RE.match(line)
        if m:
            checked += 1
            return None
        m = _MOD_ENC_BAD_RE.match(line)
        if not m:
            return None
        return {
            "file": m.group("file").strip(),
            "message": f"Invalid UTF-8 encoding: {m.group('msg').strip()}",
            "severity": "error",
        }

    return _run_parsed_script(
        script,
        mod_root,
        list(files),
        collect,
        limit=limit,
        extra=lambda proc, issues: {"checked": checked + len(issues)},
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

    def collect(line: str) -> Optional[dict]:
        m = _LOC_ENC_BAD_RE.match(line)
        if not m:
            return None
        return {
            "file": m.group("file").strip(),
            "message": "Missing UTF-8 BOM (required for HOI4 localization)",
            "severity": "error",
        }

    return _run_parsed_script(script, mod_root, args, collect, limit=limit)


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
        validators    — mod validators to merge into the same output. Omit to
                        run `style` for full-tree mode or scoped script files;
                        pass `[]` to disable validators. `["auto"]` selects by
                        the domain of the files in scope, `["*"]` runs every
                        fast validator, and explicit names run exactly those;
                        sentinels and names union. Slower than the lint scripts
                        because validators scan their whole domain.
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
        relevant: Optional[List[str]] = [_norm_scope_path(f) for f in files]
    elif mode == "all":
        relevant = None
    elif mode == "changed":
        relevant = _changed_files(mod_root)
    else:  # staged
        relevant = _staged_files(mod_root)

    relevant_set: Optional[set] = set(relevant) if relevant is not None else None

    # Expand the validators request up front; unknown names land as isolated
    # ok:false entries instead of aborting the whole run.
    # `None` and `[]` differ intentionally: omission keeps style enforcement
    # for script scopes, while an explicit empty list disables all validators.
    if validators is None:
        style_in_scope = relevant is None or any(
            f.endswith(".txt") and f.startswith(("common/", "events/", "history/"))
            for f in relevant
        )
        validator_request = ["style"] if style_in_scope else []
    else:
        validator_request = list(validators)
    validator_names: List[str] = []
    validator_setup_entries: List[dict] = []
    runner: Optional[ValidatorRunner] = None
    if validator_request:
        try:
            runner = validator_runner or ValidatorRunner(mod_root)
            available = {v.name for v in runner.list()}
        except Exception as e:
            failed_name = "validator:style" if validators is None else "validator:setup"
            validator_setup_entries.append(
                {
                    "name": failed_name,
                    "ok": False,
                    "error": f"Failed to discover validators: {e}",
                }
            )
            runner = None
        else:
            if validators is None:
                if "style" in available:
                    validator_names = ["style"]
                else:
                    validator_setup_entries.append(
                        {
                            "name": "validator:style",
                            "ok": False,
                            "error": "Default style validator is unavailable",
                        }
                    )
            else:
                expanded: set = set()
                for v in validator_request:
                    if v == "*":
                        expanded |= available - SLOW_VALIDATORS
                    elif v != "auto":
                        expanded.add(v)
                unknown_validators = sorted(expanded - available)
                for v in unknown_validators:
                    validator_setup_entries.append(
                        {
                            "name": f"validator:{v}",
                            "ok": False,
                            "error": (
                                f"Unknown validator '{v}'. "
                                f"Valid: {sorted(available)} plus 'auto' and '*'"
                            ),
                        }
                    )
                expanded -= set(unknown_validators)
                if "auto" in validator_request:
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
        "common_mistakes": lambda: _maybe(
            relevant,
            lambda: lint_common_mistakes_tool(
                mod_root,
                mode="all" if relevant is None else "staged",
                files=relevant,
            ),
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
            if result.get("stderr_tail"):
                check_summary["stderr_tail"] = result["stderr_tail"]
        else:
            issues = result.get("issues", []) or []
            # Tag each issue with which check produced it (helps the agent).
            for i in issues:
                i.setdefault("check", name)
                sev = i.get("severity", "info")
                overall[sev] = overall.get(sev, 0) + 1
            all_issues.extend(issues)
        per_check.append(check_summary)

    per_check.extend(validator_setup_entries)
    validators_ran = False
    if validator_names and runner is not None:
        validators_ran = True
        v_entries, v_issues = run_validators_for_lint(
            runner,
            validator_names,
            staged_only=(mode == "staged" and files is None),
            relevant_set=relevant_set,
            mod_root=mod_root,
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

    failed_checks = [c["name"] for c in per_check if not c.get("ok")]
    summary: dict = {
        "ok": not failed_checks,
        "mode": "files" if files is not None else mode,
        "checks_run": selected,
        "validators_run": validator_names if validators_ran else [],
        "failed_checks": failed_checks,
        "counts": overall,
        "issues_total_after_filter": len(filtered),
        "truncated": truncated,
        "checks": per_check,
    }
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


def _norm_scope_path(path: str) -> str:
    """Posix separators, no `./` prefix — the shape the prefix matchers expect."""
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _changed_files(mod_root: Path) -> List[str]:
    """Every file `git status` reports — staged, unstaged, and untracked.

    Parses `git status --porcelain -z` (NUL-terminated, so paths with spaces or
    non-ASCII arrive verbatim instead of C-quoted).
    `--untracked-files=all` lists files inside untracked directories
    individually; without it git collapses them to `?? dir/` and the files
    never reach the per-check filters.
    For renames the **new** path is returned (with `-z` it comes first, the old
    path follows as its own NUL-terminated entry).
    Deletions are skipped — there's nothing to lint for a removed file.
    Returns [] when `mod_root` isn't a git repo.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
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
    entries = iter(proc.stdout.split("\0"))
    for raw in entries:
        if len(raw) < 4:
            continue
        status = raw[:2]
        path = raw[3:]
        if "R" in status or "C" in status:
            next(entries, None)  # drop the old path that follows the new one
        # Skip deletions; nothing to lint.
        if status.strip() == "D":
            continue
        if path and path not in seen:
            seen.add(path)
            files.append(path)
    return files
