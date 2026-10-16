"""Generate a bounded localisation stub for selected focus and event ids."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import Settings
from ..indexes import EventIndex, FocusIndex, LocalisationIndex
from ..indexes.localisation import LANG_ISO_TO_SUFFIX
from ..paradox.ast_cache import parse_cached
from ..paradox.schema import find_event_nodes, node_line, symbol_or_str
from ..util.index_anchor import select_indexed_anchor
from ..util.line_numbers import line_starts
from ..util.pathing import resolve_scope_file
from ..util.response import coerce_int, enforce_budget, paginate
from .loc import generate_loc_stub


def generate_missing_loc_tool(
    settings: Settings,
    focus_index: FocusIndex,
    event_index: EventIndex,
    loc_index: LocalisationIndex,
    *,
    focus_ids: Optional[list[str]] = None,
    event_ids: Optional[list[str]] = None,
    lang: str = "en",
    limit: int = 200,
    offset: int = 0,
    bom_prefix: bool = False,
) -> dict:
    """Return missing focus/event localisation as YAML text; never writes files."""
    try:
        limit = coerce_int(limit, name="limit", default=200)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not focus_ids and not event_ids:
        return {"ok": False, "error": "Provide focus_ids= or event_ids=."}
    if lang != "en":
        return {
            "ok": False,
            "error": "generate_missing_loc currently supports English only (lang='en').",
        }

    candidates: dict[str, str] = {}
    unresolved: list[str] = []
    failures: list[str] = []
    for ident in dict.fromkeys(focus_ids or []):
        record = focus_index.resolve(ident)
        if record is None:
            unresolved.append(f"focus:{ident}")
            continue
        candidates.setdefault(ident, ident.split("_", 1)[-1].replace("_", " ").title())
        candidates.setdefault(f"{ident}_desc", "TODO: focus description.")

    for ident in dict.fromkeys(event_ids or []):
        record = event_index.resolve(ident)
        if record is None:
            unresolved.append(f"event:{ident}")
            continue
        source_path = resolve_scope_file(
            record["file"], settings.mod_root, settings.vanilla_path, settings.submod_root
        )
        if source_path is None:
            failures.append(f"event:{ident}: source file unavailable")
            continue
        try:
            _source, root = parse_cached(Path(source_path))
            nodes = find_event_nodes(root, ident)
            node = select_indexed_anchor(
                [(node_line(candidate, line_starts(_source)), candidate) for candidate in nodes],
                line=record.get("line"),
                kind="Event",
                ident=ident,
            )
        except Exception as exc:
            failures.append(f"event:{ident}: could not read event details ({type(exc).__name__})")
            continue
        hidden = symbol_or_str(node.get("hidden")) == "yes"
        for field, label, value in (
            ("title", "title", f"Event: {ident}"),
            ("desc", "description", "TODO: event description."),
        ):
            entries = node.get_all(field)
            if not entries and not hidden:
                failures.append(f"event:{ident}: {label} localisation key is missing")
            for entry in entries:
                # Triggered variants are `title = { trigger = { ... } text = KEY }`.
                key = symbol_or_str(entry) or symbol_or_str(entry.get("text"))
                if key:
                    candidates.setdefault(key, value)
                else:
                    failures.append(f"event:{ident}: {label} has no scalar or text key")
        for child in node.get_all("option"):
            raw_name = child.get("name")
            option_name = symbol_or_str(raw_name)
            if option_name:
                candidates.setdefault(
                    option_name, option_name.rpartition(".")[2].replace("_", " ").title()
                )
            else:
                # Structured/custom option names are not yet safely understood;
                # surface the omission so the generated text cannot claim completeness.
                failures.append(f"event:{ident}: option localisation key is non-scalar")

    # `[GetSomething]` is a scripted-localisation call, not a YAML key.
    missing = [
        {"key": key, "value": value}
        for key, value in sorted(candidates.items())
        if not key.startswith("[") and loc_index.resolve(key, lang) is None
    ]
    selected, page_truncated, total = paginate(missing, offset=offset, limit=limit)
    lang_suffix = LANG_ISO_TO_SUFFIX[lang]
    generated = generate_loc_stub(
        selected, lang=lang_suffix, include_header=True, bom_prefix=bom_prefix
    )
    complete = offset == 0 and not page_truncated and not unresolved and not failures
    result = {
        "ok": True,
        "lang": lang,
        "total": total,
        "returned": len(selected),
        "truncated": page_truncated,
        "complete": complete and not page_truncated,
        "partial": not complete,
        "unresolved": unresolved[:50],
        "failures": failures[:50],
        "txt": generated.get("txt", ""),
        "txt_truncated": bool(
            generated.get("txt_truncated")
            or generated.get("size_truncated")
            or generated.get("txt_dropped") is not None
        ),
        "loc_yml_keys": selected,
        "note": (
            "Generated text only; review and write it explicitly. Partial results omit "
            "unresolved or unreadable inputs."
        ),
    }
    result["complete"] = complete and not page_truncated and not result["txt_truncated"]
    result["partial"] = not result["complete"]
    bounded = enforce_budget(result, heavy_keys=("loc_yml_keys", "failures", "unresolved", "txt"))
    if (
        bounded.get("size_truncated")
        or bounded.get("loc_yml_keys_dropped") is not None
        or bounded.get("txt_dropped") is not None
    ):
        bounded["complete"] = False
        bounded["partial"] = True
    if bounded.get("loc_yml_keys_dropped") is not None:
        bounded["returned"] = 0
        bounded["truncated"] = bool(bounded.get("total", 0))
    if bounded.get("txt_dropped") is not None:
        bounded["txt_truncated"] = True
    return enforce_budget(bounded, heavy_keys=("loc_yml_keys", "failures", "unresolved", "txt"))
