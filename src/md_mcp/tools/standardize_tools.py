"""In-memory wrappers for Millennium Dawn's upstream script standardizers."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

from ..util.encoding import UTF8_BOM
from ..util.pathing import PathAccessError, validate_user_path
from ..util.response import BUDGET_BYTES, enforce_budget

SUPPORTED_KINDS: tuple[str, ...] = (
    "focus",
    "event",
    "decision",
    "idea",
    "mio",
    "technology",
    "history",
)

_MAX_TXT_BYTES = max(1, BUDGET_BYTES - 12_000)
_UPSTREAM_MODULES: tuple[str, ...] = (
    "standardize_api",
    "_common",
    "common_utils",
    "shared_utils",
    "standardize_decisions",
    "standardize_events",
    "standardize_focus_tree",
    "standardize_history",
    "standardize_ideas",
    "standardize_mio",
    "standardize_technologies",
)
_loaded_mod_root: Optional[Path] = None
_loaded_api: Optional[ModuleType] = None
_inserted_dirs: list[str] = []


def _load_standardize_api(mod_root: Path) -> ModuleType:
    """Load the upstream API and its sibling modules from this mod root."""
    global _loaded_mod_root, _loaded_api, _inserted_dirs
    root = mod_root.resolve()
    if _loaded_mod_root != root:
        for directory in _inserted_dirs:
            while directory in sys.path:
                sys.path.remove(directory)
        _inserted_dirs = []
        for name in _UPSTREAM_MODULES:
            sys.modules.pop(name, None)
        for directory_path in (root / "tools", root / "tools" / "standardization"):
            value = str(directory_path)
            if value in sys.path:
                sys.path.remove(value)
            sys.path.insert(0, value)
            _inserted_dirs.append(value)
        _loaded_api = None
        _loaded_mod_root = root

    if _loaded_api is None:
        _loaded_api = importlib.import_module("standardize_api")
    return _loaded_api


def _clip_txt(result: dict, txt: str) -> dict:
    """Fit `txt` to the JSON output budget and report any content clipping."""
    encoded = txt.encode("utf-8")
    if len(encoded) > _MAX_TXT_BYTES:
        txt = encoded[:_MAX_TXT_BYTES].decode("utf-8", "ignore")
    result.update(
        {
            "txt": txt,
            "txt_bytes": len(encoded),
            "txt_returned_bytes": len(txt.encode("utf-8")),
            "txt_truncated": len(txt.encode("utf-8")) < len(encoded),
        }
    )
    if result["txt_truncated"]:
        result["note"] = "txt clipped to fit the response budget; do NOT write clipped content back"

    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > BUDGET_BYTES:
        original = txt
        low = 0
        high = len(original)
        best: Optional[dict] = None
        while low <= high:
            middle = (low + high) // 2
            candidate_txt = original[:middle]
            candidate = {
                **result,
                "txt": candidate_txt,
                "txt_returned_bytes": len(candidate_txt.encode("utf-8")),
                "txt_truncated": True,
                "note": (
                    "txt clipped to fit the response budget; do NOT write clipped content back"
                ),
            }
            if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best is None:
            best = {
                **result,
                "txt": "",
                "txt_returned_bytes": 0,
                "txt_truncated": True,
                "note": (
                    "txt clipped to fit the response budget; do NOT write clipped content back"
                ),
            }
        result = best

    return enforce_budget(result, heavy_keys=("txt",))


def standardize_tool(
    mod_root: Path,
    *,
    content: Optional[str] = None,
    path: Optional[str] = None,
    content_type: Optional[str] = None,
) -> dict:
    """Standardize text in memory with an upstream standardizer; never write files."""
    if (content is None) == (path is None):
        return {"ok": False, "error": "Provide exactly one of content= or path=."}

    normalized_type = content_type.strip().lower() if content_type else None
    if normalized_type is not None and normalized_type not in SUPPORTED_KINDS:
        supported = ", ".join(SUPPORTED_KINDS)
        return {
            "ok": False,
            "error": (
                f"Unsupported content_type '{content_type}'. Supported types: "
                f"{supported}. Localisation is not supported."
            ),
        }
    if content is not None and normalized_type is None:
        return {
            "ok": False,
            "error": "content_type is required for content=; path= can detect the type.",
        }

    api: ModuleType
    norm_path: Optional[str] = None
    if path is not None:
        try:
            source_path = validate_user_path(
                path,
                mod_root,
                extensions={".txt"},
                require_file=True,
            )
        except (PathAccessError, OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        try:
            api = _load_standardize_api(mod_root)
        except ImportError as exc:
            return _upstream_import_error(mod_root, exc)
        try:
            norm_path = source_path.relative_to(mod_root.resolve()).as_posix()
        except ValueError:
            return {"ok": False, "error": "path must be inside the mod root."}
        kind = normalized_type or api.kind_for_path(norm_path)
        if kind is None:
            return {
                "ok": False,
                "error": f"Could not detect a standardizer kind for path '{norm_path}'.",
            }
        try:
            raw = source_path.read_bytes()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        had_bom = raw.startswith(UTF8_BOM)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return {
                "ok": False,
                "error": (f"Invalid UTF-8 at byte {exc.start} ({exc.reason}) in {norm_path}."),
            }
    else:
        kind = normalized_type
        try:
            api = _load_standardize_api(mod_root)
        except ImportError as exc:
            return _upstream_import_error(mod_root, exc)
        assert content is not None
        had_bom = content.startswith("\ufeff")
        text = content.removeprefix("\ufeff")

    if kind not in SUPPORTED_KINDS:
        return {
            "ok": False,
            "error": f"Could not detect a standardizer kind for path '{norm_path}'.",
        }

    try:
        standardized = api.standardize_text(kind, text, str(mod_root))
    except Exception as exc:
        return {"ok": False, "kind": kind, "error": str(exc)}

    txt = (text if standardized is None else standardized).removeprefix("\ufeff")
    result = {
        "ok": True,
        "kind": kind,
        "changed": txt != text or had_bom,
    }
    return _clip_txt(result, txt)


def _upstream_import_error(mod_root: Path, exc: ImportError) -> dict:
    return {
        "ok": False,
        "error": (
            f"Upstream standardization API not found under {mod_root}/tools/standardization "
            f"({exc}); is this a Millennium-Dawn checkout with the standardization tools?"
        ),
    }
