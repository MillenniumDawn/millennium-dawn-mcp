"""String-returning wrappers for the upstream lint fixers.

`Millennium-Dawn/tools/linting/fix_*.py` only run in pre-commit, so hook-less
edits bypass them entirely. This module exposes their cores as one in-memory
tool: give content or a mod-relative path, get the fixed text plus a change
summary. The server never writes mod files (CLAUDE.md rule 2) — the caller
writes the returned `txt` via Edit/Write.

Upstream pieces imported in-process (underscore API, same brittleness class as
validator `_issues`; keep both sides in mind when upstream refactors):

  * `fix_styling.fix_line`          — per-line styling fixes
  * `fix_loc_yaml.check_line` / `fix_line` — per-line loc YAML fixes
  * `check_common_mistakes._find_focus_log_mismatches` /
    `_find_decision_log_mismatches` — log-id detection (shared with the checker)

`fix_line_endings` has no importable core (its only entry point writes files),
so its CRLF→LF byte transform is reimplemented inline.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

from ..util.encoding import UTF8_BOM
from ..util.pathing import PathAccessError, validate_user_path
from ..util.response import BUDGET_BYTES, enforce_budget
from .linting_tools import _clip_utf8, _norm_scope_path

FIXERS: tuple[str, ...] = ("styling", "loc_yaml", "line_endings", "log_ids")

# fix_log_ids only rewrites inside these directories (upstream _finder_for).
LOG_ID_SCOPES: tuple[str, ...] = ("common/national_focus/", "common/decisions/")

# Leave room for status fields and warnings within the response budget.
_MAX_TXT_BYTES = max(1, BUDGET_BYTES - 12_000)

_UPSTREAM_MODULES: tuple[str, ...] = (
    "shared_utils",
    "fix_styling",
    "fix_loc_yaml",
    "check_common_mistakes",
)

_loaded_mod_root: Optional[Path] = None
_loaded_modules: Optional[dict[str, ModuleType]] = None


def _load_fixer_modules(mod_root: Path) -> dict[str, ModuleType]:
    """Import the upstream fixer cores, re-importing when mod_root changes.

    Modules are cached per mod_root so a server process imports once; tests
    that plant stand-ins in fresh tmp trees get a fresh import instead of a
    stale sys.modules hit from an earlier root.
    """
    global _loaded_mod_root, _loaded_modules
    if _loaded_modules is not None and _loaded_mod_root == mod_root:
        return _loaded_modules

    for d in (str(mod_root / "tools"), str(mod_root / "tools" / "linting")):
        if d not in sys.path:
            sys.path.insert(0, d)
    for name in _UPSTREAM_MODULES:
        sys.modules.pop(name, None)
    modules = {name: importlib.import_module(name) for name in _UPSTREAM_MODULES}
    _loaded_mod_root = mod_root
    _loaded_modules = modules
    return modules


def fix_lint_tool(
    mod_root: Path,
    *,
    fixer: str,
    path: Optional[str] = None,
    content: Optional[str] = None,
) -> dict:
    """Apply one upstream lint fixer in-memory and return the fixed text.

    Args:
        fixer   — `styling` | `loc_yaml` | `line_endings` | `log_ids`
        path    — mod-relative path. Source when `content` is omitted; always
                  required for `log_ids` (scope detection is path-based);
                  `loc_yaml` is restricted to .yml (its tab→space rewrite
                  corrupts .txt) and `styling` to .txt. Pure metadata when
                  `content` is supplied.
        content — text to fix; no file access at all when given.
    """
    if fixer not in FIXERS:
        return {"ok": False, "error": f"Unknown fixer '{fixer}'. Valid: {list(FIXERS)}"}

    norm_path = _norm_scope_path(path) if path else None
    if content is None and norm_path is None:
        return {"ok": False, "error": "Provide content= or path=."}
    if fixer == "log_ids" and norm_path is None:
        return {
            "ok": False,
            "error": "fixer=log_ids requires path= (scope detection is path-based)",
        }

    modules = _load_fixer_modules(mod_root)

    raw: bytes
    if content is not None:
        source = "content"
        had_bom = content.startswith("\ufeff")
        raw = (content[1:] if had_bom else content).encode("utf-8")
    else:
        source = "path"
        assert norm_path is not None  # guaranteed by the checks above
        if fixer == "loc_yaml" and not norm_path.endswith(".yml"):
            return {
                "ok": False,
                "error": f"fixer=loc_yaml only applies to .yml files, got {norm_path}",
            }
        if fixer == "styling" and not norm_path.endswith(".txt"):
            return {
                "ok": False,
                "error": f"fixer=styling only applies to .txt files, got {norm_path}",
            }
        try:
            resolved = validate_user_path(norm_path, mod_root, require_file=True)
        except PathAccessError as e:
            return {"ok": False, "error": str(e)}
        raw = resolved.read_bytes()
        had_bom = raw.startswith(UTF8_BOM)
        if had_bom:
            raw = raw[len(UTF8_BOM) :]

    result: dict = {
        "ok": True,
        "fixer": fixer,
        "file": norm_path,
        "source": source,
    }
    if had_bom:
        result["had_bom"] = True

    if fixer == "line_endings":
        return _finish_line_endings(result, raw)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return _decode_error(norm_path, e)

    if fixer == "log_ids":
        assert norm_path is not None
        return _fix_log_ids(result, modules, text, norm_path)
    if fixer == "styling":
        fixed, fixes, summary, warnings = _fix_styling(modules, text)
    else:
        fixed, fixes, summary, warnings = _fix_loc_yaml(modules, text)
    return _finish(result, text, fixed, fixes, summary, warnings)


def _decode_error(file: Optional[str], e: UnicodeDecodeError) -> dict:
    """Strict decode failure — never fix around U+FFFD-substituted bytes."""
    where = f" in {file}" if file else ""
    return {
        "ok": False,
        "error": (
            f"Invalid UTF-8 at byte {e.start} ({e.reason}){where}; refusing to "
            f"fix around bad bytes — run check_encoding or lint first."
        ),
    }


def _fix_styling(
    modules: dict[str, ModuleType],
    text: str,
) -> tuple[str, int, dict, list[str]]:
    """Per-line styling fixes, mirroring upstream fix_file's line loop."""
    fix_line = modules["fix_styling"].fix_line
    strip_inline_comment = modules["shared_utils"].strip_inline_comment

    lines = text.split("\n")
    fixed_lines: list[str] = []
    total_fixes = 0
    lines_fixed = 0
    warnings: list[str] = []

    for line_num, line in enumerate(lines, 1):
        fixed, fixes = fix_line(line)
        total_fixes += fixes
        if fixes:
            lines_fixed += 1
        fixed_lines.append(fixed)

        if '"' in line and not line.strip().startswith("#"):
            code = strip_inline_comment(line) if "#" in line else line
            if code.count('"') % 2 == 1:
                warnings.append(f"line {line_num}: possible missing quotation mark")

    while fixed_lines and fixed_lines[-1] == "":
        fixed_lines.pop()
    fixed_lines.append("")

    summary = {"lines_fixed": lines_fixed, "line_fixes": total_fixes}
    return "\n".join(fixed_lines), total_fixes, summary, warnings


def _fix_loc_yaml(
    modules: dict[str, ModuleType],
    text: str,
) -> tuple[str, int, dict, list[str]]:
    """Per-line loc YAML fixes; counts come from upstream's own detector."""
    check_line = modules["fix_loc_yaml"].check_line
    fix_line = modules["fix_loc_yaml"].fix_line

    counts: dict[str, int] = {}
    fixed_lines: list[str] = []
    for i, line in enumerate(text.split("\n")):
        for _, issue_type, _desc in check_line(line, i + 1):
            counts[issue_type] = counts.get(issue_type, 0) + 1
        fixed_lines.append(fix_line(line))

    return "\n".join(fixed_lines), sum(counts.values()), counts, []


def _finish(
    result: dict,
    original: str,
    fixed: str,
    fixes: int,
    summary: dict,
    warnings: list[str],
) -> dict:
    """Shape a styling/loc_yaml result. Fixers may detect problems they cannot
    rewrite (e.g. loc_yaml's missing_close_quote), so `changed` comes from a
    content comparison, not from the detection counts."""
    result["fixes"] = fixes
    result["summary"] = summary
    if warnings:
        result["warnings"] = warnings
    result["changed"] = fixed != original
    if not result["changed"]:
        return enforce_budget(result, heavy_keys=("txt",))
    return _emit_txt(result, fixed)


def _finish_line_endings(result: dict, raw: bytes) -> dict:
    """CRLF→LF at byte level (upstream parity), then decode for the response."""
    count = raw.count(b"\r\n")
    result["changed"] = count > 0
    result["fixes"] = count
    result["summary"] = {"crlf_to_lf": count}
    if count == 0:
        return enforce_budget(result, heavy_keys=("txt",))

    try:
        text = raw.replace(b"\r\n", b"\n").decode("utf-8")
    except UnicodeDecodeError as e:
        # The fix itself succeeded, but the caller can't use it as text.
        return {
            "ok": False,
            "fixer": result["fixer"],
            "file": result.get("file"),
            "error": (
                f"Invalid UTF-8 at byte {e.start} ({e.reason}); the CRLF→LF fix "
                f"applied but the file is not valid UTF-8 — run "
                f"tools/linting/fix_line_endings.py manually."
            ),
        }
    return _emit_txt(result, text)


def _fix_log_ids(
    result: dict,
    modules: dict[str, ModuleType],
    text: str,
    norm_path: str,
) -> dict:
    """Rewrite mismatched focus/decision log ids via upstream detection."""
    if "common/national_focus" in norm_path:
        kind = "focus"
    elif "common/decisions" in norm_path:
        kind = "decision"
    else:
        result.update(
            {
                "changed": False,
                "fixes": 0,
                "summary": {},
                "note": f"path outside fixer scope ({', '.join(LOG_ID_SCOPES)})",
            }
        )
        return enforce_budget(result, heavy_keys=("txt",))

    finder = getattr(modules["check_common_mistakes"], f"_find_{kind}_log_mismatches")
    lines = text.splitlines(keepends=True)
    mismatches = finder(lines)
    if not mismatches:
        result.update({"changed": False, "fixes": 0, "summary": {f"{kind}_log_ids": 0}})
        return enforce_budget(result, heavy_keys=("txt",))

    by_line: dict[int, list[tuple[int, int, str]]] = {}
    for line_idx, start, end, correct_id, _bad_token in mismatches:
        by_line.setdefault(line_idx, []).append((start, end, correct_id))

    for line_idx, spans in by_line.items():
        line = lines[line_idx]
        # Rightmost first, so earlier spans stay valid after length changes.
        for start, end, replacement in sorted(spans, reverse=True):
            line = line[:start] + replacement + line[end:]
        lines[line_idx] = line

    count = len(mismatches)
    result.update({"changed": True, "fixes": count, "summary": {f"{kind}_log_ids": count}})
    return _emit_txt(result, "".join(lines))


def _emit_txt(result: dict, txt: str) -> dict:
    txt, total, returned, truncated = _clip_utf8(txt, _MAX_TXT_BYTES)
    result.update(
        {
            "txt": txt,
            "txt_bytes": total,
            "txt_returned_bytes": returned,
            "txt_truncated": truncated,
        }
    )
    if truncated:
        result["note"] = (
            "txt clipped to fit the response budget; do NOT write clipped "
            "content back — edit the file directly instead"
        )
    return enforce_budget(result, heavy_keys=("txt",))
