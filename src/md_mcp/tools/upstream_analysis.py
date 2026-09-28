from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..util.response import coerce_int, enforce_budget

_SHIM = Path(__file__).with_name("upstream_analysis_shim.py")
_TICK_TIMEOUT = 120
_GDP_TIMEOUT = 120
_CALENDAR_TIMEOUT = 10


def _run_shim(mod_root: Path, operation: str, payload: dict, *, timeout: int) -> dict:
    try:
        proc = subprocess.run(
            [sys.executable, str(_SHIM), operation, str(mod_root), json.dumps(payload)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
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


def calculate_days_tool(mod_root: Path, year: int, month: int, day: int) -> dict:
    """Calculate days since 2000; uses fixed non-leap years and validates year/month/day."""
    result = _run_shim(
        mod_root,
        "calculate_days",
        {"year": year, "month": month, "day": day},
        timeout=_CALENDAR_TIMEOUT,
    )
    return enforce_budget(result)
