from __future__ import annotations

import difflib
import html
import re
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..util.response import coerce_int, enforce_budget, paginate

_DOC_KINDS = frozenset(("effect", "trigger", "modifier"))
_DOC_RELATIVE_PATHS = {
    "effect": Path("resources/documentation/effects_documentation.md"),
    "trigger": Path("resources/documentation/triggers_documentation.md"),
    "modifier": Path("resources/documentation/modifiers_documentation.md"),
}
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_SPAN_RE = re.compile(r"<span\b[^>]*>.*?</span>", re.IGNORECASE)
_SCOPE_HEADING_RE = re.compile(r"^(?:effects|triggers|modifiers) for scope\b", re.IGNORECASE)


def lookup_docs_tool(
    settings: Settings,
    kind: str,
    key: Optional[str] = None,
    limit: int | float | str | None = 100,
    offset: int | float | str | None = 0,
) -> dict:
    normalized_kind = kind.lower() if isinstance(kind, str) else kind
    result_context = {"kind": kind}
    if key is not None:
        result_context["key"] = key

    if normalized_kind not in _DOC_KINDS:
        return enforce_budget(
            {
                "ok": False,
                **result_context,
                "error": "kind must be one of: effect, trigger, modifier",
            }
        )

    try:
        limit = coerce_int(limit, name="limit", default=100)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return enforce_budget({"ok": False, **result_context, "error": str(exc)})

    relative_path = _DOC_RELATIVE_PATHS[normalized_kind]
    path = settings.mod_root / relative_path
    if not path.is_file():
        return enforce_budget(
            {
                "ok": False,
                **result_context,
                "file": relative_path.as_posix(),
                "error": f"Documentation file not found: {relative_path.as_posix()}",
            }
        )

    try:
        entries = _read_entries(path, relative_path.as_posix())
    except (OSError, UnicodeError) as exc:
        return enforce_budget(
            {
                "ok": False,
                **result_context,
                "file": relative_path.as_posix(),
                "error": f"Could not read documentation: {exc}",
            }
        )

    if key is not None:
        definitions = entries.get(key)
        if definitions is None:
            suggestions = _suggestions(key, entries)
            page, truncated, total = paginate(suggestions, offset=offset, limit=limit)
            return enforce_budget(
                {
                    "ok": False,
                    **result_context,
                    "file": relative_path.as_posix(),
                    "error": f"No {normalized_kind} documentation found for {key!r}",
                    "total": total,
                    "returned": len(page),
                    "truncated": truncated,
                    "suggestions": page,
                },
                heavy_keys=("suggestions",),
            )

        page, truncated, total = paginate(definitions, offset=offset, limit=limit)
        first = definitions[0]
        return enforce_budget(
            {
                "ok": True,
                "kind": normalized_kind,
                "key": key,
                "file": first["file"],
                "line": first["line"],
                "total": total,
                "returned": len(page),
                "truncated": truncated,
                "entries": page,
            },
            heavy_keys=("entries",),
        )

    summaries = [_entry_summary(definitions[0]) for definitions in entries.values()]
    page, truncated, total = paginate(summaries, offset=offset, limit=limit)
    return enforce_budget(
        {
            "ok": True,
            "kind": normalized_kind,
            "total": total,
            "returned": len(page),
            "truncated": truncated,
            "entries": page,
        },
        heavy_keys=("entries",),
    )


def _read_entries(path: Path, relative_path: str) -> dict[str, list[dict]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    headings: list[tuple[int, str, bool]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        heading = _clean_heading(match.group(1))
        headings.append((index, heading, _is_entry_heading(heading)))

    entries: dict[str, list[dict]] = {}
    for position, (start, key, is_entry) in enumerate(headings):
        if not is_entry:
            continue
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        body = "\n".join(lines[start + 1 : end]).strip()
        entry = {
            "key": key,
            "content": body,
            "file": relative_path,
            "line": start + 1,
            "end_line": end,
        }
        entries.setdefault(key, []).append(entry)
    return entries


def _entry_summary(entry: dict) -> dict:
    return {key: entry[key] for key in ("key", "file", "line", "end_line")}


def _clean_heading(heading: str) -> str:
    return html.unescape(_SPAN_RE.sub("", heading)).strip()


def _is_entry_heading(heading: str) -> bool:
    return heading.lower() != "table of content" and not _SCOPE_HEADING_RE.match(heading)


def _suggestions(key: str, entries: dict[str, list[dict]]) -> list[str]:
    keys = list(entries)
    lowered = {candidate.lower(): candidate for candidate in keys}
    matches = difflib.get_close_matches(key.lower(), lowered, n=5, cutoff=0.5)
    return [lowered[match] for match in matches]
