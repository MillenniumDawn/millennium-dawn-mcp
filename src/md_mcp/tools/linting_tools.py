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

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..analysis.suppressions import SUPPRESSION_SOURCE, suppressed_count
from ..util.pathing import contained
from ..util.process import run_in_group
from ..util.response import BUDGET_BYTES, MAX_TEXT_BYTES, clip_utf8, enforce_budget
from ..validators import SLOW_VALIDATORS, ValidatorRunner, count_severities
from ..validators.attribution import normalize_path
from .lint_validators import (
    EQUIPMENT_VARIANT_PREFIXES,
    STYLE_PREFIXES,
    run_validators_for_lint,
    select_validators,
)
from .validation_tools import filter_and_cap

_LINT_LINE_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):\s*(?P<msg>.+)$")

# validate_mod_encoding emits one line per file on stdout/stderr.
_MOD_ENC_OK_RE = re.compile(r"^(?P<file>.+?):\s+Valid UTF-8 encoding\s*$")
_MOD_ENC_BAD_RE = re.compile(r"^(?P<file>.+?):\s+Invalid UTF-8 encoding\s+-\s+(?P<msg>.+)$")

# validate_localization_encoding emits the BOM-missing diagnostic.
_LOC_ENC_BAD_RE = re.compile(r"^(?P<file>.+?):\s+Missing UTF-8 BOM.*$")

# ANSI escape stripper for scripts that emit colour codes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _review_payload(
    base: str,
    *,
    proc: Optional[subprocess.CompletedProcess] = None,
    error: Optional[str] = None,
) -> dict:
    """Shape and guard a review report on both subprocess paths."""
    if proc is None:
        bounded_error, _, _, _ = clip_utf8(str(error or ""), 2_000)
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
        report, report_bytes, returned_bytes, report_truncated = clip_utf8(
            raw_report, MAX_TEXT_BYTES
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


def _failure_payload(proc: subprocess.CompletedProcess, error: str) -> dict:
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
        return _failure_payload(proc, f"{script.name} crashed mid-run (traceback on stderr)")

    if proc.returncode == 0 or (proc.returncode == 1 and issue_count):
        return None

    if proc.returncode == 1:
        error = f"{script.name} exited with code 1 without recognized diagnostics"
    else:
        error = f"{script.name} exited with unexpected code {proc.returncode}"
    return _failure_payload(proc, error)


def lint_common_mistakes_tool(
    mod_root: Path,
    *,
    mode: str = "staged",
    files: Optional[list[str]] = None,
    submod_root: Optional[Path] = None,
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
        return {
            "file": file_path,
            "line": int(m.group("line")),
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
        cwd=submod_root,
    )


def review_branch_tool(
    mod_root: Path, base: str = "main", *, submod_root: Optional[Path] = None
) -> dict:
    """Run `tools/analysis/review_branch.py` and return a bounded text summary.

    The script produces a human-readable digest (commits, file diffs, content
    summary). The report is clipped by UTF-8 bytes, with its original and returned
    sizes exposed so callers can request a narrower review when needed.
    """
    script = mod_root / "tools" / "analysis" / "review_branch.py"
    proc, err = _run_script(script, mod_root, [base], cwd=submod_root)
    if proc is None:
        return _review_payload(base, error=err)
    return _review_payload(base, proc=proc)


def _run_script(
    script: Path,
    mod_root: Path,
    args: list[str],
    *,
    timeout: int = 120,
    cwd: Optional[Path] = None,
) -> "tuple[Optional[subprocess.CompletedProcess], Optional[str]]":
    """Subprocess a tools/ script. Returns (proc, error_msg). One of them is None."""
    if not script.exists():
        return None, f"{script.name} not found at {script}"
    try:
        proc = run_in_group(
            [sys.executable, str(script), *args],
            cwd=str(cwd or mod_root),
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
    args: list[str],
    collect: Callable[[str], Optional[dict]],
    *,
    timeout: int = 120,
    combined: bool = True,
    limit: Optional[int] = 200,
    extra: Optional[Callable[[subprocess.CompletedProcess, list[dict]], dict]] = None,
    cwd: Optional[Path] = None,
) -> dict:
    """Run a script, parse its output, and shape its issue response."""
    proc, err = _run_script(script, mod_root, args, timeout=timeout, cwd=cwd)
    if proc is None:
        return {"ok": False, "error": err}

    output = (proc.stdout or "") + "\n" + (proc.stderr or "") if combined else proc.stdout or ""
    issues: list[dict] = []
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


def _discover_mod_files(mod_root: Path, submod_root: Optional[Path]) -> list[str]:
    """List `.mod` arguments overlay first; base-only ones are absolute (cwd is the overlay)."""
    if submod_root is None:
        return sorted(p.name for p in mod_root.glob("*.mod") if p.is_file())
    overlay = sorted(p.name for p in submod_root.glob("*.mod") if p.is_file())
    base = [str(p) for p in sorted(mod_root.glob("*.mod")) if p.is_file() and p.name not in overlay]
    return overlay + base


def lint_mod_encoding_tool(
    mod_root: Path,
    *,
    files: Optional[list[str]] = None,
    limit: int = 200,
    submod_root: Optional[Path] = None,
) -> dict:
    """Run `tools/linting/validate_mod_encoding.py` against `.mod` files.

    With no `files`, defaults to every `.mod` file under the mod root, with
    overlay descriptors shadowing base ones of the same name.
    """
    script = mod_root / "tools" / "linting" / "validate_mod_encoding.py"
    if files is None:
        files = _discover_mod_files(mod_root, submod_root)
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
        cwd=submod_root,
    )


def lint_loc_encoding_tool(
    mod_root: Path,
    *,
    files: Optional[list[str]] = None,
    limit: int = 200,
    submod_root: Optional[Path] = None,
) -> dict:
    """Run `tools/linting/validate_localization_encoding.py` (English loc YAML BOM check).

    With no `files`, the script auto-discovers every English loc YAML.
    `--fix` is **never** passed — this tool is read-only by design; use
    Edit/Write to add BOMs.
    """
    script = mod_root / "tools" / "linting" / "validate_localization_encoding.py"
    args: list[str] = list(files) if files else []

    def collect(line: str) -> Optional[dict]:
        m = _LOC_ENC_BAD_RE.match(line)
        if not m:
            return None
        return {
            "file": m.group("file").strip(),
            "message": "Missing UTF-8 BOM (required for HOI4 localization)",
            "severity": "error",
        }

    return _run_parsed_script(script, mod_root, args, collect, limit=limit, cwd=submod_root)


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
    submod_root: Optional[Path] = None,
    mode: str = "changed",
    files: Optional[list[str]] = None,
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
        files         — explicit mod-relative or contained absolute paths. Each
                        checker filters by its own file pattern.
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
    removed_paths: list[str] = []
    relevant: Optional[list[str]]
    try:
        if files is not None:
            explicit_files: list[str] = []
            for file in files:
                normalized = normalize_path(file)
                if Path(normalized).is_absolute():
                    resolved = contained(mod_root, normalized)
                    if resolved is None:
                        return {"ok": False, "error": f"{file!r} is outside the mod root"}
                    normalized = resolved.relative_to(mod_root.resolve()).as_posix()
                explicit_files.append(normalized)
            relevant = explicit_files
        elif mode == "all":
            relevant = None
        elif mode == "changed":
            relevant = _changed_files(mod_root, submod_root, removed=removed_paths)
        else:  # staged
            relevant = _staged_files(mod_root, submod_root)
    except GitScopeError as exc:
        return {
            "ok": False,
            "error": f"Git {exc.mode} scope discovery failed: {exc.reason}",
            "scope_error": exc.as_dict(),
        }

    relevant_set: Optional[set] = set(relevant) if relevant is not None else None
    removed_variant_paths = [
        path
        for path in removed_paths
        if path.endswith(".txt") and path.startswith(EQUIPMENT_VARIANT_PREFIXES)
    ]
    validator_relevant_set = (
        relevant_set | set(removed_variant_paths) if relevant_set is not None else None
    )
    # Git includes staged deletions in the scope, but file-based scripts cannot
    # inspect a missing path. Explicit files= keeps its existing behavior.
    present_relevant = (
        [
            path
            for path in relevant
            if (mod_root / path).is_file()
            or (submod_root is not None and (submod_root / path).is_file())
        ]
        if files is None and relevant is not None
        else relevant
    )
    # Upstream common_mistakes and style scan only these; images and .gfx just cost time.
    script_files = (
        [f for f in present_relevant if f.endswith(".txt") and f.startswith(STYLE_PREFIXES)]
        if present_relevant is not None
        else None
    )

    # Expand the validators request up front; unknown names land as isolated
    # ok:false entries instead of aborting the whole run.
    # `None` and `[]` differ intentionally: omission keeps style enforcement
    # for script scopes, while an explicit empty list disables all validators.
    if validators is None:
        style_in_scope = script_files is None or bool(script_files)
        validator_request = ["style"] if style_in_scope else []
    else:
        validator_request = list(validators)
    validator_names: list[str] = []
    validator_setup_entries: list[dict] = []
    runner: Optional[ValidatorRunner] = None
    if validator_request:
        try:
            runner = validator_runner or ValidatorRunner(mod_root, submod_root=submod_root)
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
                    expanded |= set(select_validators(relevant, available, mod_root=mod_root))
                    if removed_variant_paths and "equipment_variants" in available:
                        expanded.add("equipment_variants")
                validator_names = sorted(expanded)

    if present_relevant is not None:
        mod_files: Optional[list[str]] = [f for f in present_relevant if f.endswith(".mod")]
        loc_files: Optional[list[str]] = [
            f
            for f in present_relevant
            if f.startswith("localisation/english/") and f.endswith(".yml")
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

    def _maybe(files_list: Optional[list[str]], runner: Callable[[], dict]) -> dict:
        if files_list is not None and not files_list:
            return _skipped()
        return runner()

    runners: dict[str, Callable[[], dict]] = {
        "common_mistakes": lambda: _maybe(
            script_files,
            lambda: lint_common_mistakes_tool(
                mod_root,
                mode="all" if script_files is None else "staged",
                files=script_files,
                submod_root=submod_root,
            ),
        ),
        "mod_encoding": lambda: _maybe(
            mod_files,
            lambda: lint_mod_encoding_tool(mod_root, files=mod_files, submod_root=submod_root),
        ),
        "loc_encoding": lambda: _maybe(
            loc_files,
            lambda: lint_loc_encoding_tool(mod_root, files=loc_files, submod_root=submod_root),
        ),
    }

    per_check: list[dict] = []
    all_issues: list[dict] = []
    suppressed_total = 0

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
            relevant_set=validator_relevant_set,
            mod_root=mod_root,
        )
        per_check.extend(v_entries)
        all_issues.extend(v_issues)
        suppressed_total += sum(suppressed_count(entry) for entry in v_entries)

    issues_capped, truncated, issues_total = filter_and_cap(
        all_issues, severity_min=severity_min, limit=limit
    )

    failed_checks = [c["name"] for c in per_check if not c.get("ok")]
    summary: dict = {
        "ok": not failed_checks,
        "mode": "files" if files is not None else mode,
        "checks_run": selected,
        "validators_run": validator_names if validators_ran else [],
        "failed_checks": failed_checks,
        "counts": count_severities(all_issues),
        "issues_total_after_filter": issues_total,
        "truncated": truncated,
        "checks": per_check,
    }
    if suppressed_total:
        summary["suppressed_mod_wide"] = suppressed_total
        summary["suppression_source"] = SUPPRESSION_SOURCE
    if not counts_only:
        summary["issues"] = issues_capped

        # Keep a useful prefix of diagnostics when the byte budget is tighter
        # than the caller's issue limit. enforce_budget() drops the whole array
        # once it is oversized, which would hide every consumer location.
        def fits_budget() -> bool:
            return (
                len(json.dumps(summary, ensure_ascii=False, default=str).encode("utf-8"))
                <= BUDGET_BYTES
            )

        if not fits_budget():
            summary["truncated"] = True
            low, high = 0, len(issues_capped)
            while low < high:
                middle = (low + high + 1) // 2
                summary["issues"] = issues_capped[:middle]
                if fits_budget():
                    low = middle
                else:
                    high = middle - 1
            summary["issues"] = issues_capped[:low]

    return enforce_budget(summary, heavy_keys=("issues",))


_GIT_SCOPE_TIMEOUT = 15
_GIT_SCOPE_REASON_BYTES = 500
_GIT_SCOPE_STDERR_BYTES = 1_000


def _split_utf8_prefix_length(encoded: bytes, tail_start: int, tail_bytes: bytes) -> int:
    """Bytes at the tail's start that finish a codepoint split at its byte boundary."""
    for prefix_length in range(1, min(3, tail_start) + 1):
        prefix = encoded[tail_start - prefix_length : tail_start]
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.end != len(prefix):
                continue
            partial = prefix[exc.start :]
            for tail_length in range(1, min(3, len(tail_bytes)) + 1):
                try:
                    (partial + tail_bytes[:tail_length]).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                return tail_length
    return 0


def _scope_text(value: object, max_bytes: int, *, tail: bool = False) -> str:
    """Convert subprocess output to a bounded, UTF-8-safe string."""
    if tail:
        encoded = (
            value
            if isinstance(value, bytes)
            else str(value or "").encode("utf-8", errors="replace")
        )
        tail_start = max(0, len(encoded) - max_bytes)
        tail_bytes = encoded[tail_start:]
        offset = _split_utf8_prefix_length(encoded, tail_start, tail_bytes)
        tail_text = tail_bytes[offset:].decode("utf-8", errors="replace")
        # Replacements for malformed internal bytes can expand the decoded
        # tail. Trim from its front so the most recent stderr remains visible.
        while len(tail_text.encode("utf-8")) > max_bytes:
            tail_text = tail_text[1:]
        return tail_text
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    return clip_utf8(text, max_bytes)[0]


class GitScopeError(RuntimeError):
    """A Git command failed while resolving the files a lint run should inspect."""

    def __init__(
        self,
        *,
        mode: str,
        command: Sequence[str],
        reason: str,
        exit_code: Optional[int] = None,
        stderr: object = "",
    ) -> None:
        self.mode = mode
        self.command = tuple(command)
        self.reason = _scope_text(reason, _GIT_SCOPE_REASON_BYTES)
        self.exit_code = exit_code
        self.stderr_tail = _scope_text(stderr, _GIT_SCOPE_STDERR_BYTES, tail=True)
        super().__init__(self.reason)

    def as_dict(self) -> dict:
        """Return a bounded scope-discovery error for the MCP response."""
        return {
            "mode": self.mode,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "reason": self.reason,
            "stderr_tail": self.stderr_tail,
        }


def _run_git_scope(
    mode: str,
    command: list[str],
    mod_root: Path,
    submod_root: Optional[Path],
) -> subprocess.CompletedProcess:
    """Run one Git scope-discovery command or raise a structured error."""
    try:
        proc = subprocess.run(
            command,
            cwd=str(submod_root or mod_root),
            capture_output=True,
            text=True,
            timeout=_GIT_SCOPE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitScopeError(
            mode=mode,
            command=command,
            reason=f"Git command timed out after {_GIT_SCOPE_TIMEOUT}s",
            stderr=exc.stderr,
        ) from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise GitScopeError(
            mode=mode,
            command=command,
            reason=f"Could not run Git command: {exc}",
        ) from exc

    if proc.returncode != 0:
        raise GitScopeError(
            mode=mode,
            command=command,
            reason=f"Git exited with status {proc.returncode}",
            exit_code=proc.returncode,
            stderr=proc.stderr,
        )
    return proc


def _staged_files(mod_root: Path, submod_root: Optional[Path] = None) -> list[str]:
    """Staged files in the active worktree's git index; renames list both paths."""
    proc = _run_git_scope(
        "staged",
        ["git", "diff", "--name-only", "-z", "--cached", "--no-renames"],
        mod_root,
        submod_root,
    )
    return [path for path in proc.stdout.split("\0") if path]


def _changed_files(
    mod_root: Path,
    submod_root: Optional[Path] = None,
    *,
    removed: Optional[list[str]] = None,
) -> list[str]:
    """Every file `git status` reports — staged, unstaged, and untracked.

    Parses `git status --porcelain -z` (NUL-terminated, so paths with spaces or
    non-ASCII arrive verbatim instead of C-quoted).
    `--untracked-files=all` lists files inside untracked directories
    individually; without it git collapses them to `?? dir/` and the files
    never reach the per-check filters.
    For renames the **new** path is returned (with `-z` it comes first, the old
    path follows as its own NUL-terminated entry).
    Deletions are skipped — there's nothing to lint for a removed file. When
    `removed` is supplied, deleted paths and rename sources are appended there
    for validators whose context can change when a file disappears.
    A Git failure raises `GitScopeError`; a successful empty status is a valid no-op.
    """
    proc = _run_git_scope(
        "changed",
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        mod_root,
        submod_root,
    )

    files: list[str] = []
    seen: set = set()
    entries = iter(proc.stdout.split("\0"))
    for raw in entries:
        if len(raw) < 4:
            continue
        status = raw[:2]
        path = raw[3:]
        if "R" in status or "C" in status:
            old_path = next(entries, None)
            if "R" in status and removed is not None and old_path:
                removed.append(old_path)
        # Skip deletions; nothing to lint.
        if status.strip() == "D":
            if removed is not None and path:
                removed.append(path)
            continue
        if path and path not in seen:
            seen.add(path)
            files.append(path)
    return files
