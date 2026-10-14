from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from ..util.response import enforce_budget

_SHIM = Path(__file__).with_name("ci_timing_shim.py")
_TIMEOUT_SECONDS = 60


def _sync_budget_metadata(bounded: dict) -> dict:
    dropped_keys = bounded.get("dropped_keys", [])
    if not isinstance(dropped_keys, list):
        dropped_keys = []
    for name in ("samples", "groups"):
        dropped = f"{name}_dropped" in bounded or name in dropped_keys
        if dropped:
            bounded[f"{name}_returned"] = 0
            bounded[f"{name}_truncated"] = True
        elif isinstance(bounded.get(name), list):
            bounded[f"{name}_returned"] = len(bounded[name])
        elif bounded.get("ok") is True and f"{name}_total" in bounded:
            # The last-resort budget fallback may omit both the list and its
            # per-list dropped count while retaining only a dropped_keys list.
            bounded[f"{name}_returned"] = 0
            bounded[f"{name}_truncated"] = True
    return bounded


def validation_timing_summary_tool(
    mod_root: Path,
    content: str,
    limit: int = 20,
    offset: int = 0,
    include_steps: bool = False,
) -> dict:
    """Summarize supplied Actions timing exports through the upstream parser."""
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a JSON string"}
    try:
        content_bytes = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return {"ok": False, "error": "content contains invalid Unicode text"}
    if len(content_bytes) > 1_000_000:
        return {"ok": False, "error": "content exceeds 1000000 UTF-8 bytes"}
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
    ):
        return {"ok": False, "error": "limit and offset must be integers"}
    if not isinstance(include_steps, bool):
        return {"ok": False, "error": "include_steps must be a boolean"}
    payload = {
        "content": content,
        "limit": limit,
        "offset": offset,
        "include_steps": include_steps,
    }
    try:
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False)
        proc = subprocess.Popen(
            [sys.executable, str(_SHIM), str(mod_root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(encoded, timeout=_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
            else:
                proc.kill()
            proc.communicate()
            return {"ok": False, "error": "Timing analysis exceeded its 60 second limit."}
    except (OSError, ValueError, UnicodeError) as exc:
        return {"ok": False, "error": f"Could not start timing analysis: {exc}"}
    if proc.returncode != 0:
        detail = (stderr or "").strip()[-500:]
        return {
            "ok": False,
            "error": "Timing analysis subprocess failed" + (f": {detail}" if detail else ""),
        }
    try:
        result = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Timing analysis returned invalid JSON: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "error": "Timing analysis returned a non-object response"}
    bounded = enforce_budget(result, heavy_keys=("samples", "groups"))
    return _sync_budget_metadata(bounded)
