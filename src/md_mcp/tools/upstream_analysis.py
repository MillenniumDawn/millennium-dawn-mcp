from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..util.process import run_in_group
from ..util.response import coerce_int, enforce_budget

_FOCUS_OVERLAP_TIMEOUT = 120
_SHIM = Path(__file__).with_name("upstream_analysis_shim.py")
_TICK_TIMEOUT = 120
_GDP_TIMEOUT = 120
_EVENT_LOAD_TIMEOUT = 180
_AI_PATH_TIMEOUT = 240
_TICK_SPOT_TIMEOUT = 240
_DAYS_PER_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _run_shim(mod_root: Path, operation: str, payload: dict, *, timeout: int) -> dict:
    try:
        proc = run_in_group(
            [sys.executable, str(_SHIM), operation, str(mod_root), json.dumps(payload)],
            timeout=timeout,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": f"{operation} timed out after {timeout}s: {exc}"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"Could not run {operation}: {exc}"}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-1000:]
        error = f"{operation} subprocess exited with code {proc.returncode}"
        if detail:
            error = f"{error}: {detail}"
        return {"ok": False, "error": error}

    try:
        result = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"{operation} returned invalid JSON: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "error": f"{operation} returned a non-object response"}
    return result


def tick_audit_tool(
    mod_root: Path,
    *,
    tag: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Summarize recurring tick work; tag filters hooks and limit/offset page hook samples."""
    try:
        limit = coerce_int(limit, name="limit", default=20)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return enforce_budget({"ok": False, "error": str(exc)}, heavy_keys=("hooks",))
    result = _run_shim(
        mod_root,
        "tick_audit",
        {"tag": tag.upper() if tag else None, "limit": limit, "offset": offset},
        timeout=_TICK_TIMEOUT,
    )
    bounded = enforce_budget(result, heavy_keys=("hooks",))
    if "hooks_dropped" in bounded and bounded["hooks_dropped"] != 0:
        bounded["returned"] = 0
        bounded["truncated"] = True
    return enforce_budget(bounded, heavy_keys=())


def tick_spot_check_tool(
    mod_root: Path,
    files: list[str],
    *,
    submod_root: Optional[Path] = None,
    scope: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Run upstream spot checks on explicitly selected changed files."""
    try:
        limit = coerce_int(limit, name="limit", default=100)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return enforce_budget(
            {"ok": False, "error": str(exc)}, heavy_keys=("findings", "diagnostics")
        )
    if not files:
        return {"ok": False, "error": "Provide at least one changed file in files=."}
    result = _run_shim(
        mod_root,
        "tick_spot_check",
        {
            "files": files,
            "scan_root": str(submod_root) if submod_root is not None else None,
            "scope": scope,
            "limit": limit,
            "offset": offset,
        },
        timeout=_TICK_SPOT_TIMEOUT,
    )
    result = enforce_budget(result, heavy_keys=("findings", "diagnostics", "assumptions"))
    if result.get("size_truncated"):
        # The budget guard may remove a whole page. Keep the pagination fields
        # consistent with the lists that are actually present in the response.
        for key, total_key, returned_key, truncated_key in (
            ("findings", "total", "returned", "truncated"),
            ("diagnostics", "diagnostics_total", "diagnostics_returned", "diagnostics_truncated"),
        ):
            dropped = result.get(f"{key}_dropped")
            if dropped is not None:
                result[returned_key] = 0
                result[truncated_key] = bool(result.get(total_key, dropped))
    return enforce_budget(result, heavy_keys=("findings", "diagnostics", "assumptions"))


def estimate_gdp_tool(mod_root: Path, tag: str) -> dict:
    """Estimate one country's starting GDP from upstream state history and ideas."""
    result = _run_shim(
        mod_root,
        "estimate_gdp",
        {"tag": tag.upper()},
        timeout=_GDP_TIMEOUT,
    )
    return enforce_budget(result, heavy_keys=("breakdown",))


def event_load_tool(
    mod_root: Path,
    tag: str = "USA",
    window: int = 45,
    threshold: int = 3,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Summarize scheduled event totals, busy windows, and flagged years."""
    result = _run_shim(
        mod_root,
        "event_load",
        {
            "tag": tag,
            "window": window,
            "threshold": threshold,
            "limit": limit,
            "offset": offset,
        },
        timeout=_EVENT_LOAD_TIMEOUT,
    )
    bounded = enforce_budget(result, heavy_keys=("years", "flagged_years"))
    if "years_dropped" in bounded and bounded["years_dropped"] != 0:
        bounded["returned"] = 0
        bounded["truncated"] = True
    elif "flagged_years_dropped" in bounded and bounded["flagged_years_dropped"] != 0:
        bounded["truncated"] = True
    return enforce_budget(bounded, heavy_keys=())


def game_log_summary_tool(
    mod_root: Path,
    path: str,
    *,
    top: int = 15,
    limit: int = 100,
    offset: int = 0,
    countries: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    """Summarize one absolute game log path without modifying it."""
    try:
        top = coerce_int(top, name="top", default=15)
        limit = coerce_int(limit, name="limit", default=100)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return enforce_budget({"ok": False, "path": path, "error": str(exc)})
    result = _run_shim(
        mod_root,
        "game_log_summary",
        {
            "path": path,
            "top": top,
            "limit": limit,
            "offset": offset,
            "countries": countries,
            "since": since,
            "until": until,
        },
        timeout=120,
    )
    bounded = enforce_budget(
        result,
        heavy_keys=(
            "focus_countries",
            "economy",
            "inflation",
            "conflicts",
            "politics",
            "annexations",
        ),
    )
    for key in (
        "focus_countries",
        "economy",
        "inflation",
        "conflicts",
        "politics",
        "annexations",
    ):
        if f"{key}_dropped" not in bounded:
            continue
        if bounded[f"{key}_dropped"] != 0:
            bounded[f"{key}_returned"] = 0
            bounded[f"{key}_truncated"] = True
    return enforce_budget(bounded, heavy_keys=())


def ai_path_report_tool(
    mod_root: Path,
    tag: str,
    section: Optional[str | list[str]] = None,
    limit: int = 15,
    offset: int = 0,
) -> dict:
    """Report one country's AI path with section selection and paged lists."""
    try:
        limit = coerce_int(limit, name="limit", default=15)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return enforce_budget({"ok": False, "tag": tag, "error": str(exc)})
    result = _run_shim(
        mod_root,
        "ai_path_report",
        {"tag": tag, "section": section, "limit": limit, "offset": offset},
        timeout=_AI_PATH_TIMEOUT,
    )
    return enforce_budget(
        result, heavy_keys=("pagination", "matrix", "graph", "plans", "mechanics")
    )


def focus_overlap_scenarios_tool(
    mod_root: Path,
    file: str,
    tag: str,
    *,
    submod_root: Optional[Path] = None,
    completed: Optional[list[str]] = None,
    date: Optional[str] = None,
    include_positions: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Report same-cell focus groups for one explicit gameplay scenario."""

    def error_result(message: str, *, include_file: bool = True) -> dict:
        result = {"ok": False, "error": message}
        if include_file and isinstance(file, str):
            result["file"] = file
        return enforce_budget(result)

    try:
        limit = min(100, coerce_int(limit, name="limit", default=50))
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return error_result(str(exc))
    if not isinstance(file, str) or not file.strip():
        return error_result("file must name one explicit focus-tree .txt file", include_file=False)
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9]{2,4}", tag):
        return error_result("tag must be a 2-4 character country tag")
    if completed is None:
        completed = []
    if (
        not isinstance(completed, list)
        or len(completed) > 200
        or any(not isinstance(value, str) or not value or len(value) > 200 for value in completed)
    ):
        return error_result("completed must be a list of at most 200 focus ids")
    if not isinstance(include_positions, bool):
        return error_result("include_positions must be a boolean")
    if date is not None and (not isinstance(date, str) or len(date) > 32):
        return error_result("date must be a YYYY.M.D string")
    try:
        source_path, path_error = _focus_source_path(mod_root, submod_root, file)
    except (OSError, ValueError) as exc:
        return error_result(f"Could not resolve focus file: {exc}")
    if path_error or source_path is None:
        return error_result(path_error or "focus file could not be resolved")
    result = _run_shim(
        mod_root,
        "focus_overlap_scenarios",
        {
            "file": str(source_path),
            "allowed_roots": [str(root) for root in (submod_root, mod_root) if root],
            "tag": tag.upper(),
            "completed": completed,
            "date": date,
            "include_positions": include_positions,
            "limit": limit,
            "offset": offset,
        },
        timeout=_FOCUS_OVERLAP_TIMEOUT,
    )
    heavy_keys = (("positions",) if result.get("positions_included") else ()) + (
        "collision_groups",
        "diagnostics",
    )
    shaped = enforce_budget(result, heavy_keys=heavy_keys)
    for _ in range(len(heavy_keys) + 1):
        metadata_changed = False
        for name in ("positions", "collision_groups", "diagnostics"):
            dropped_key = f"{name}_dropped"
            if dropped_key in shaped and (
                shaped.get(f"{name}_returned") != 0 or shaped.get(f"{name}_truncated") is not True
            ):
                shaped[f"{name}_returned"] = 0
                shaped[f"{name}_truncated"] = True
                metadata_changed = True
        if not metadata_changed:
            return shaped
        shaped = enforce_budget(shaped, heavy_keys=heavy_keys)
    return enforce_budget(shaped, heavy_keys=heavy_keys)


def _focus_source_path(
    mod_root: Path, submod_root: Optional[Path], value: str
) -> tuple[Optional[Path], Optional[str]]:
    roots: list[Path] = []
    for root_value in (submod_root, mod_root):
        if root_value is None:
            continue
        root = root_value.resolve(strict=True)
        if root.is_dir():
            roots.append(root)
    if not roots:
        return None, "configured mod roots are unavailable"

    requested = Path(value)
    candidates = [requested] if requested.is_absolute() else [root / requested for root in roots]
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            inside = any(resolved.is_relative_to(root) for root in roots)
        except (OSError, ValueError):
            continue
        linked = candidate.is_symlink()
        parent = candidate.parent
        while not linked and parent != parent.parent:
            linked = parent.is_symlink()
            if parent in roots:
                break
            parent = parent.parent
        if inside and not linked and resolved.is_file() and resolved.suffix.casefold() == ".txt":
            if resolved.stat().st_size > 2_000_000:
                return None, "focus file exceeds the 2 MB analysis limit"
            return resolved, None
    return None, "file must be an existing regular .txt file inside a configured mod root"


def calculate_days_tool(year: int, month: int, day: int) -> dict:
    """Calculate days since 2000; uses fixed non-leap years and validates year/month/day."""
    return enforce_budget(_calculate_days(year, month, day))


def _calculate_days(year: object, month: object, day: object) -> dict:
    if (
        isinstance(year, bool)
        or not isinstance(year, int)
        or isinstance(month, bool)
        or not isinstance(month, int)
        or isinstance(day, bool)
        or not isinstance(day, int)
    ):
        return {"ok": False, "error": "year, month, and day must be integers"}
    if year < 2000:
        return {"ok": False, "error": "year must be at least 2000"}
    if month < 1 or month > 12:
        return {"ok": False, "error": "month must be between 1 and 12"}
    if day < 1 or day > _DAYS_PER_MONTH[month - 1]:
        return {"ok": False, "error": "day is outside the selected month"}
    days = (year - 2000) * sum(_DAYS_PER_MONTH)
    days += sum(_DAYS_PER_MONTH[: month - 1])
    days += day - 1
    return {"ok": True, "days": days}
