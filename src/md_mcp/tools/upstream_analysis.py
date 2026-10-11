from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..util.process import run_in_group
from ..util.response import coerce_int, enforce_budget

_SHIM = Path(__file__).with_name("upstream_analysis_shim.py")
_TICK_TIMEOUT = 120
_GDP_TIMEOUT = 120
_AI_PATH_TIMEOUT = 240
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
    return enforce_budget(result, heavy_keys=("hooks",))


def estimate_gdp_tool(mod_root: Path, tag: str) -> dict:
    """Estimate one country's starting GDP from upstream state history and ideas."""
    result = _run_shim(
        mod_root,
        "estimate_gdp",
        {"tag": tag.upper()},
        timeout=_GDP_TIMEOUT,
    )
    return enforce_budget(result, heavy_keys=("breakdown",))


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
