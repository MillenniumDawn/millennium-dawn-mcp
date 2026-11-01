"""Generate a bounded localisation stub for selected focus and event ids."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import Settings
from ..indexes import EventIndex, FocusIndex, LocalisationIndex
from ..indexes.localisation import LANG_ISO_TO_SUFFIX
from ..paradox.ast_cache import parse_cached
from ..paradox.nodes import node_line, symbol_or_str
from ..paradox.schema import (
    extract_decision_records,
    extract_event_records,
    extract_focus_records,
    extract_idea_records,
    find_event_nodes,
)
from ..util.index_anchor import select_indexed_anchor
from ..util.line_numbers import line_starts
from ..util.pathing import contained, is_under_vanilla, resolve_scope_file, roots_for
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
    file: Optional[str] = None,
    target_file: Optional[str] = None,
) -> dict:
    """Return missing focus/event localisation as YAML text; never writes files."""
    try:
        limit = coerce_int(limit, name="limit", default=200)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if file:
        if focus_ids or event_ids:
            return {"ok": False, "error": "Use file= or focus_ids=/event_ids=, not both."}
        if lang != "en":
            return {
                "ok": False,
                "error": "generate_missing_loc currently supports English only (lang='en').",
            }
        return _generate_file_missing_loc(settings, loc_index, file, target_file, limit, offset)
    if target_file:
        return {"ok": False, "error": "target_file= requires file=."}
    if not focus_ids and not event_ids:
        return {"ok": False, "error": "Provide file=, focus_ids= or event_ids=."}
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
            source = Path(source_path)
            _source, root = parse_cached(
                source,
                close_blocks_at_eof=is_under_vanilla(source, settings.vanilla_path),
            )
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


def _generate_file_missing_loc(
    settings: Settings,
    loc_index: LocalisationIndex,
    file: str,
    target_file: Optional[str],
    limit: int,
    offset: int,
) -> dict:
    """Generate bounded file-scoped keys, using AST projections and read-only inputs."""
    source_path = resolve_scope_file(
        file, settings.mod_root, settings.vanilla_path, settings.submod_root
    )
    if source_path is None:
        return {"ok": False, "error": f"Source file not found within configured roots: {file}"}
    try:
        source = Path(source_path)
        text, root = parse_cached(
            source, close_blocks_at_eof=is_under_vanilla(source, settings.vanilla_path)
        )
    except Exception as exc:
        return {"ok": False, "error": f"Could not parse source file ({type(exc).__name__})."}

    rel_parts = tuple(part.lower() for part in source.parts)
    candidates: dict[str, str] = {}
    failures: list[str] = []
    if "national_focus" in rel_parts:
        for rec in extract_focus_records(root, text):
            ident = rec["id"]
            candidates.setdefault(ident, ident.rsplit("_", 1)[-1].replace("_", " ").title())
            candidates.setdefault(f"{ident}_desc", "TODO: focus description.")
    elif "events" in rel_parts:
        for rec in extract_event_records(root, text):
            ident = rec["id"]
            for node in find_event_nodes(root, ident):
                hidden = symbol_or_str(node.get("hidden")) == "yes"
                for field, value in (
                    ("title", f"Event: {ident}"),
                    ("desc", "TODO: event description."),
                ):
                    if not node.get_all(field) and not hidden:
                        failures.append(f"event:{ident}: {field} localisation key is missing")
                        continue
                    for entry in node.get_all(field):
                        key = symbol_or_str(entry) or symbol_or_str(entry.get("text"))
                        if key:
                            candidates.setdefault(key, value)
                        else:
                            failures.append(f"event:{ident}: {field} has no scalar or text key")
                for option in node.get_all("option"):
                    key = symbol_or_str(option.get("name"))
                    if key:
                        candidates.setdefault(key, key.rpartition(".")[2].replace("_", " ").title())
                    else:
                        failures.append(f"event:{ident}: option localisation key is non-scalar")
    elif "ideas" in rel_parts or any(child.name == "ideas" for child in root.children()):
        for rec in extract_idea_records(root, text):
            ident = rec["id"]
            candidates.setdefault(ident, ident.replace("_", " ").title())
            candidates.setdefault(f"{ident}_desc", "TODO: idea description.")
    elif "decisions" in rel_parts:
        decisions = extract_decision_records(root, text)
        categories = [
            node for node in root.children() if node.name and isinstance(node.value, list)
        ]
        if decisions or categories:
            for rec in decisions:
                ident = rec["id"]
                candidates.setdefault(ident, ident.replace("_", " ").title())
                candidates.setdefault(f"{ident}_desc", "TODO: decision description.")
            for category in categories:
                ident = category.name
                if ident is None:
                    continue
                candidates.setdefault(ident, ident.replace("_", " ").title())
                candidates.setdefault(f"{ident}_desc", "TODO: category description.")

    if not candidates:
        return {
            "ok": False,
            "error": "Source file type is unsupported or contains no supported definitions.",
        }

    target_keys: set[str] = set()
    target_exists = False
    if target_file:
        target_name = Path(target_file).name.lower()
        target_parts = {part.lower() for part in Path(target_file).parts}
        if not target_name.endswith("_l_english.yml") or not {
            "localisation",
            "english",
        }.issubset(target_parts):
            return {
                "ok": False,
                "error": (
                    "target_file must be an English localisation path ending in _l_english.yml."
                ),
            }
        target_path = resolve_scope_file(target_file, settings.mod_root, None, settings.submod_root)
        if target_path is None:
            target_path = next(
                (
                    path
                    for root_path in roots_for(settings.mod_root, None, settings.submod_root)
                    if (path := contained(root_path, target_file)) is not None
                ),
                None,
            )
            if target_path is None:
                return {"ok": False, "error": "target_file escapes configured content roots."}
        else:
            target_exists = True
            try:
                target_text = target_path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                return {
                    "ok": False,
                    "error": f"Could not read target localisation file ({type(exc).__name__}).",
                }
            from ..indexes.localisation import _parse_loc_file

            target_keys = {
                entry["key"] for entry in _parse_loc_file(target_text, target_path.name)["keys"]
            }
    missing = [
        {"key": key, "value": value}
        for key, value in sorted(candidates.items())
        if not key.startswith("[")
        and key not in target_keys
        and loc_index.resolve(key, "en") is None
    ]
    selected, truncated, total = paginate(missing, offset=offset, limit=limit)
    generated = generate_loc_stub(
        selected,
        lang="l_english",
        include_header=not target_exists,
        bom_prefix=not target_exists,
    )
    txt = generated.get("txt", "")
    txt_truncated = bool(
        generated.get("txt_truncated")
        or generated.get("size_truncated")
        or generated.get("txt_dropped") is not None
    )
    result = {
        "ok": True,
        "file": file,
        "target_file": target_file,
        "lang": "en",
        "total": total,
        "returned": len(selected),
        "truncated": truncated,
        "complete": offset == 0 and not truncated and not failures and not txt_truncated,
        "partial": offset != 0 or truncated or bool(failures) or txt_truncated,
        "target_exists": target_exists,
        "bom_prefix": not target_exists,
        "txt": txt,
        "txt_truncated": txt_truncated,
        "loc_yml_keys": selected,
        "failures": failures[:50],
        "note": "Generated text only; review and write it explicitly.",
    }
    bounded = enforce_budget(result, heavy_keys=("loc_yml_keys", "failures", "txt"))
    if bounded.get("size_truncated") or bounded.get("loc_yml_keys_dropped") is not None:
        bounded["complete"] = False
        bounded["partial"] = True
    if bounded.get("txt_dropped") is not None:
        bounded["txt_truncated"] = True
        bounded["complete"] = False
        bounded["partial"] = True
    if bounded.get("loc_yml_keys_dropped") is not None:
        bounded["returned"] = 0
        bounded["truncated"] = bool(bounded.get("total", 0))
    return enforce_budget(bounded, heavy_keys=("loc_yml_keys", "failures", "txt"))
