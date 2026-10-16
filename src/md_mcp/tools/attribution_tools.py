from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..util.process import run_in_group
from ..util.response import enforce_budget

_SHIM = Path(__file__).with_name("attribution_shim.py")
_TIMEOUT_SECONDS = 30


def check_attribution_labels_tool(
    mod_root: Path,
    content: str,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Report explicit attribution labels from supplied text without echoing it."""
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}
    try:
        content_bytes = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return {"ok": False, "error": "content contains invalid Unicode text"}
    if len(content_bytes) > 1_000_000:
        return {"ok": False, "error": "content exceeds 1000000 UTF-8 bytes"}
    if "\x00" in content:
        return {"ok": False, "error": "content must not contain NUL bytes"}
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
    ):
        return {"ok": False, "error": "limit and offset must be integers"}
    payload = {"content": content, "limit": limit, "offset": offset}
    try:
        proc = run_in_group(
            [sys.executable, str(_SHIM), str(mod_root)],
            timeout=_TIMEOUT_SECONDS,
            encoding="utf-8",
            input=json.dumps(payload, ensure_ascii=True, allow_nan=False),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Attribution analysis exceeded its 30 second limit."}
    except (OSError, ValueError, UnicodeError):
        return {"ok": False, "error": "Could not start attribution analysis subprocess."}
    if proc.returncode != 0:
        return {"ok": False, "error": "Attribution analysis subprocess failed."}
    try:
        result = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError):
        return {"ok": False, "error": "Attribution analysis returned invalid JSON."}
    if not isinstance(result, dict):
        return {"ok": False, "error": "Attribution analysis returned a non-object response"}
    return enforce_budget(result, heavy_keys=("findings",))
