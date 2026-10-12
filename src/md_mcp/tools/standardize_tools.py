"""In-memory wrappers for Millennium Dawn's upstream script standardizers.

Script kinds go through `standardize_api.standardize_text`; localisation goes
through the pure cores of upstream `standardize_localisation.py`
(`_parse_loc_file` / `_format_output`) plus a bounded scan of `CATEGORY_CONFIG`.
`LocalisationStandardizer` itself is never invoked — its `standardize_file`
entry point writes files. See docs/standardize-localisation.md.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import ModuleType
from typing import Iterator, Optional

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
    "localisation",
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
    "standardize_localisation",
    "standardize_mio",
    "standardize_technologies",
)
_MAX_LOCALISATION_INDEX_FILES = 5_000
_MAX_LOCALISATION_INDEX_BYTES = 256 * 1024 * 1024
_UPSTREAM = UpstreamModules("standardization", _UPSTREAM_MODULES)


def _load_standardize_api(mod_root: Path) -> ModuleType:
    """Load the upstream API and its sibling modules from this mod root."""
    return _UPSTREAM.load(mod_root, ("standardize_api",))["standardize_api"]


def _load_localisation_core(mod_root: Path) -> ModuleType:
    """Load upstream standardize_localisation (pure cores only) from this mod root."""
    return _UPSTREAM.load(mod_root, ("standardize_localisation",))["standardize_localisation"]


def _clip_txt(result: dict, txt: str) -> dict:
    """Fit `txt` to the JSON output budget and report any content clipping."""
    note = "txt clipped to fit the response budget; do NOT write clipped content back"
    if result.get("note"):
        note = f"{result['note']}; {note}"
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


def _bounded_localisation_index(
    loc: ModuleType, mod_root: Path, submod_root: Optional[Path] = None
) -> dict:
    """Build upstream's category index with strict file-count and byte caps.

    A submod file shadows the base-mod file at the same relative path, so the
    caps count the effective file set.
    """
    index: dict[str, set[str]] = {category: set() for category in loc.SECTION_ORDER}
    roots = [r for r in (submod_root, mod_root) if r is not None]
    scanned: dict[str, str] = {}
    total_bytes = 0

    def files(directory: Path, recursive: bool) -> Iterator[Path]:
        if not directory.is_dir():
            return
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    candidate = Path(entry.path)
                    if recursive and entry.is_dir(follow_symlinks=False):
                        yield from files(candidate, True)
                    elif entry.is_file() and candidate.suffix in {".txt", ".gui"}:
                        yield candidate
        except OSError:
            return

    for category, (dirs, patterns, recursive) in loc.CATEGORY_CONFIG.items():
        compiled = [re.compile(pattern, re.MULTILINE) for pattern in patterns]
        for rel_dir in dirs:
            for root in roots:
                for source in files(root / rel_dir, recursive):
                    rel = source.relative_to(root).as_posix()
                    if rel in scanned:
                        content = scanned[rel]
                    else:
                        if len(scanned) >= _MAX_LOCALISATION_INDEX_FILES:
                            raise ValueError(
                                "Localisation index scan exceeded "
                                f"{_MAX_LOCALISATION_INDEX_FILES} files."
                            )
                        scanned[rel] = ""
                        try:
                            size = source.stat().st_size
                            if total_bytes + size > _MAX_LOCALISATION_INDEX_BYTES:
                                raise ValueError(
                                    "Localisation index scan exceeded "
                                    f"{_MAX_LOCALISATION_INDEX_BYTES} bytes."
                                )
                            total_bytes += size
                            content = source.read_text(encoding="utf-8-sig", errors="replace")
                        except OSError:
                            continue
                        scanned[rel] = content
                    for regex in compiled:
                        for match in regex.finditer(content):
                            token = match.group(1)
                            if token:
                                index[category].add(token)
    return index


def _localisation_categories(
    loc: ModuleType, entries: list, file_stem: str, index: dict
) -> dict[str, int]:
    """Per-section key counts, mirroring upstream standardize_file's summary."""
    tag = loc._extract_tag(file_stem)
    anchor = f"{tag}_focus_tree" if tag else None
    raw: dict[str, int] = {}
    for entry in entries:
        if not entry.key or entry.key == anchor:
            continue
        category = loc._find_category(entry.key, index, None)
        raw[category] = raw.get(category, 0) + 1
    ordered = {cat: raw[cat] for cat in loc.SECTION_ORDER if cat in raw}
    return {cat: raw[cat] for cat in raw if cat not in ordered} | ordered


def _standardize_localisation(
    mod_root: Path,
    text: str,
    file_stem: str,
    had_bom: bool,
    norm_path: Optional[str],
    submod_root: Optional[Path] = None,
) -> dict:
    """Reorganise one localisation file in memory; never writes.

    The categorization index follows upstream's CATEGORY_CONFIG with a bounded
    scan. Upstream's whole-mod reference scan is skipped, so its Unreferenced
    bucket stays empty. Contract in docs/standardize-localisation.md.
    """
    try:
        loc = _load_localisation_core(mod_root)
    except ImportError as exc:
        return _upstream_import_error(mod_root, exc)

    lines = text.splitlines()
    header_index = next(
        (i for i, line in enumerate(lines) if re.match(r"^\s*l_[A-Za-z0-9_]+\s*:", line)),
        None,
    )
    if header_index is None:
        return {
            "ok": False,
            "kind": "localisation",
            "error": "No language header found (expected `l_english:`).",
        }
    # Upstream expects the header first; keep any author comments above it.
    prefix = lines[:header_index]
    try:
        index = _bounded_localisation_index(loc, mod_root, submod_root)
        header, entries = loc._parse_loc_file("\n".join(lines[header_index:]))
        standardized = loc._format_output(header, entries, index, file_stem, None)
        categories = _localisation_categories(loc, entries, file_stem, index)
    except Exception as exc:
        return {"ok": False, "kind": "localisation", "error": str(exc)}
    if prefix:
        standardized = "\n".join(prefix) + "\n" + standardized

    txt = standardized.removeprefix("\ufeff")
    result: dict = {
        "ok": True,
        "kind": "localisation",
        "changed": txt != text or had_bom,
        "categories": categories,
    }
    if norm_path is not None:
        result["file"] = norm_path
    if had_bom:
        result["had_bom"] = True
        result["note"] = (
            "input had a UTF-8 BOM and the standardizer output has none; upstream "
            "writes localisation with a UTF-8 BOM — keep the BOM when writing the file back"
        )
    return _clip_txt(result, txt)


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
            "error": f"Unsupported content_type '{content_type}'. Supported types: {supported}.",
        }
    if content is not None and normalized_type is None:
        return {
            "ok": False,
            "error": "content_type is required for content=; path= can detect the type.",
        }

    norm_path: Optional[str] = None
    file_stem = ""
    if path is not None:
        roots = [r for r in (submod_root, mod_root) if r is not None]
        try:
            source_path = validate_user_path(
                path,
                roots,
                extensions={".txt", ".yml"},
                require_file=True,
            )
        except (PathAccessError, OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        # validate_user_path guarantees the path sits under one of the roots
        owner = next(r for r in roots if source_path.is_relative_to(r.resolve()))
        norm_path = source_path.relative_to(owner.resolve()).as_posix()
        file_stem = source_path.stem
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
        # Upstream kind_for_path routes .txt paths only; .yml belongs to the
        # localisation standardizer.
        kind = normalized_type or ("localisation" if norm_path.lower().endswith(".yml") else None)
        if kind is None:
            try:
                kind = _load_standardize_api(mod_root).kind_for_path(norm_path)
            except ImportError as exc:
                return _upstream_import_error(mod_root, exc)
    else:
        assert normalized_type is not None  # checked above for content mode
        kind = normalized_type
        assert content is not None
        had_bom = content.startswith("\ufeff")
        text = content.removeprefix("\ufeff")

    if kind not in SUPPORTED_KINDS:
        return {
            "ok": False,
            "error": f"Could not detect a standardizer kind for path '{norm_path}'.",
        }

    if kind == "localisation":
        if norm_path is not None and not norm_path.lower().endswith(".yml"):
            return {
                "ok": False,
                "error": (
                    f"The localisation standardizer applies to .yml files, got '{norm_path}'."
                ),
            }
        return _standardize_localisation(mod_root, text, file_stem, had_bom, norm_path, submod_root)

    try:
        api = _load_standardize_api(mod_root)
    except ImportError as exc:
        return _upstream_import_error(mod_root, exc)
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
