from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 90_000
MAX_PAGE_SIZE = 100
SUPPORTED_RULES = {"ai-trailer", "ai-header", "ai-footer"}


def _load_helper(root: Path) -> Any:
    sys.dont_write_bytecode = True
    tools = root / "tools"
    helper = tools / "analysis" / "check_ai_attribution.py"
    if not helper.is_file():
        raise FileNotFoundError("upstream attribution helper is unavailable")
    sys.path.insert(0, str(tools))
    spec = importlib.util.spec_from_file_location("md_mcp_check_ai_attribution", helper)
    if spec is None or spec.loader is None:
        raise ImportError("upstream attribution helper could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "classify", None)) or not callable(
        getattr(module, "findings", None)
    ):
        raise ImportError("upstream attribution helper lacks classify/findings APIs")
    return module


def analyze(root: Path, payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "error": "request must be an object"}
    content = payload.get("content")
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}
    try:
        if len(content.encode("utf-8", errors="strict")) > MAX_INPUT_BYTES:
            return {"ok": False, "error": f"content exceeds {MAX_INPUT_BYTES} UTF-8 bytes"}
    except UnicodeEncodeError:
        return {"ok": False, "error": "content contains invalid Unicode text"}
    if "\x00" in content:
        return {"ok": False, "error": "content must not contain NUL bytes"}
    limit = payload.get("limit", 50)
    offset = payload.get("offset", 0)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
    ):
        return {"ok": False, "error": "limit and offset must be integers"}
    limit = min(MAX_PAGE_SIZE, max(0, limit))
    offset = max(0, offset)
    try:
        module = _load_helper(root)
        found = module.findings(content, "supplied text")
        if not isinstance(found, (list, tuple)):
            return {"ok": False, "unsupported": True, "error": "unsupported findings result"}
        rows = []
        for item in found:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 3
                or item[0] != "supplied text"
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
                or not 1 <= item[1] <= content.count("\n") + 1
                or not isinstance(item[2], str)
                or item[2] not in SUPPORTED_RULES
            ):
                return {"ok": False, "unsupported": True, "error": "unsupported findings result"}
            rows.append({"line": item[1], "rule": item[2]})
    except FileNotFoundError:
        return {
            "ok": False,
            "unsupported": True,
            "error": "upstream attribution helper is unavailable",
        }
    except ImportError:
        return {
            "ok": False,
            "unsupported": True,
            "error": "upstream attribution helper API is unavailable",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"attribution analysis failed ({type(exc).__name__})",
        }

    rule_counts = dict(sorted(Counter(row["rule"] for row in rows).items()))
    selected = rows[offset : offset + limit]
    result = {
        "ok": True,
        "report_only": True,
        "scope": "explicit labels in supplied text; no authorship inference or enforcement",
        "findings_total": len(rows),
        "findings_returned": len(selected),
        "findings_truncated": offset + limit < len(rows),
        "rule_counts": rule_counts,
        "findings": selected,
        "warnings": [
            "Only explicit attribution labels are reported; this does not infer authorship.",
            "Results are informational and do not enforce policy or change files.",
        ],
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT_BYTES:
        # Findings are compact, but unknown upstream rule strings still need a
        # hard, truthful last-line output cap.
        while (
            selected
            and len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT_BYTES
        ):
            selected.pop()
            result["findings_returned"] = len(selected)
            result["findings_truncated"] = True
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT_BYTES:
            return {
                "ok": False,
                "error": "attribution summary exceeded output byte budget",
                "findings_total": len(rows),
                "findings_returned": 0,
                "findings_truncated": True,
            }
    return result


def main() -> int:
    sys.dont_write_bytecode = True
    try:
        root = Path(sys.argv[1])
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES * 8 + 100_000)
        payload = json.loads(raw)
        result = analyze(root, payload)
    except Exception as exc:
        result = {"ok": False, "error": f"attribution analysis failed ({type(exc).__name__})"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
