"""In-memory wrappers for Millennium Dawn's upstream script standardizers."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Optional

from ..util.encoding import UTF8_BOM
from ..util.pathing import PathAccessError, validate_user_path
from ..util.response import MAX_TEXT_BYTES, clip_utf8, enforce_budget, fit_prefix
from ..util.upstream_modules import UpstreamModules

SUPPORTED_KINDS: tuple[str, ...] = (
    "focus",
    "event",
    "decision",
    "idea",
    "mio",
    "technology",
    "history",
)

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
_UPSTREAM = UpstreamModules("standardization", _UPSTREAM_MODULES)


def _load_standardize_api(mod_root: Path) -> ModuleType:
    """Load the upstream API and its sibling modules from this mod root."""
    return _UPSTREAM.load(mod_root, ("standardize_api",))["standardize_api"]


def _clip_txt(result: dict, txt: str) -> dict:
    """Fit `txt` to the JSON output budget and report any content clipping."""
    note = "txt clipped to fit the response budget; do NOT write clipped content back"
    txt, total, returned, truncated = clip_utf8(txt, MAX_TEXT_BYTES)
    result.update(
        {
            "txt": txt,
            "txt_bytes": total,
            "txt_returned_bytes": returned,
            "txt_truncated": truncated,
        }
    )
    if truncated:
        result["note"] = note

    # Escaping can inflate txt past the budget; keep the longest prefix that fits.
    def shrunk(chars: int) -> dict:
        prefix = txt[:chars]
        return {
            **result,
            "txt": prefix,
            "txt_returned_bytes": len(prefix.encode("utf-8")),
            "txt_truncated": True,
            "note": note,
        }

    return enforce_budget(fit_prefix(result, len(txt), shrunk), heavy_keys=("txt",))


def standardize_tool(
    mod_root: Path,
    submod_root: Optional[Path] = None,
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
        roots = [r for r in (submod_root, mod_root) if r is not None]
        try:
            source_path = validate_user_path(
                path,
                roots,
                extensions={".txt"},
                require_file=True,
            )
        except (PathAccessError, OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        try:
            api = _load_standardize_api(mod_root)
        except ImportError as exc:
            return _upstream_import_error(mod_root, exc)
        # validate_user_path guarantees the path sits under one of the roots
        owner = next(r for r in roots if source_path.is_relative_to(r.resolve()))
        norm_path = source_path.relative_to(owner.resolve()).as_posix()
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
