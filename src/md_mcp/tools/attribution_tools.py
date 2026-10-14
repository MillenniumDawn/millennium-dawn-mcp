from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

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
            return {"ok": False, "error": "Attribution analysis exceeded its 30 second limit."}
    except (OSError, ValueError, UnicodeError) as exc:
        return {"ok": False, "error": f"Could not start attribution analysis: {exc}"}
    if proc.returncode != 0:
        detail = (stderr or "").strip()[-300:]
        return {
            "ok": False,
            "error": "Attribution analysis subprocess failed" + (f": {detail}" if detail else ""),
        }
    try:
        result = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Attribution analysis returned invalid JSON: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "error": "Attribution analysis returned a non-object response"}
    return enforce_budget(result, heavy_keys=("findings",))
