"""Resolvers — "where is X defined?" lookups backed by the indexes.

Each resolver returns a structured record with file path, line number, and parsed
metadata where available. Returns `{ok: False, ...}` on miss so the agent can handle
the absence without exceptions.
"""

from __future__ import annotations

from typing import Optional

from ..config import Settings
from ..indexes import (
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
)
from ..paradox import parse_string
from ..paradox.schema import extract_focus_records
from ..util.encoding import read_text
from ..util.pathing import resolve_scope_file


def resolve_focus_tool(focus_id: str, settings: Settings, focus_index: FocusIndex) -> dict:
    """Get full focus definition + file/line by ID.

    Returns a record with the cached `id, file, line, kind`, plus a freshly-parsed
    detailed projection (`prerequisites`, `mutually_exclusive`, `x`, `y`, `cost`,
    `icon`, `relative_position_id`) from the file on disk.
    """
    cached = focus_index.resolve(focus_id)
    if cached is None:
        return {"ok": False, "id": focus_id, "error": "Focus not found in mod or vanilla"}

    abs_path = resolve_scope_file(cached["file"], settings.mod_root, settings.vanilla_path)
    if abs_path is None:
        return {
            "ok": True,
            "id": focus_id,
            "file": cached["file"],
            "line": cached["line"],
            "kind": cached["kind"],
            "warning": "File listed in index but no longer on disk",
        }

    try:
        text = read_text(abs_path)
        root = parse_string(text, error_prefix=f"In file {cached['file']}:\n")
        records = extract_focus_records(root, source=text)
    except Exception as e:
        return {
            "ok": True,
            "id": focus_id,
            "file": cached["file"],
            "line": cached["line"],
            "kind": cached["kind"],
            "warning": f"Could not parse file for detail extraction: {e}",
        }

    detail = next((r for r in records if r["id"] == focus_id), None)
    if detail is None:
        return {
            "ok": True,
            "id": focus_id,
            "file": cached["file"],
            "line": cached["line"],
            "kind": cached["kind"],
            "warning": "Focus disappeared from file since index was built — rerun stale check",
        }

    return {
        "ok": True,
        "id": focus_id,
        "file": cached["file"],
        "line": detail["line"] or cached["line"],
        "kind": cached["kind"],
        "parsed": {
            "x": detail["x"],
            "y": detail["y"],
            "cost": detail["cost"],
            "icon": detail["icon"],
            "prerequisites": detail["prerequisites"],
            "mutually_exclusive": detail["mutually_exclusive"],
            "relative_position_id": detail["relative_position_id"],
        },
    }


def resolve_loc_tool(
    key: str,
    settings: Settings,
    loc_index: LocalisationIndex,
    lang: Optional[str] = None,
) -> dict:
    """Get loc string + file/line for a key.

    Falls back to English if the key is missing in the requested language (matches
    the VSCode extension behaviour).
    """
    chosen_lang = lang or settings.default_lang
    rec = loc_index.resolve(key, chosen_lang)
    if rec is None:
        return {"ok": False, "key": key, "lang": chosen_lang, "error": "Loc key not found"}
    return {
        "ok": True,
        "key": rec["key"],
        "lang": rec["lang"],
        "value": rec["value"],
        "file": rec["file"],
        "line": rec["line"],
    }


def resolve_sprite_tool(name: str, settings: Settings, gfx_index: GfxIndex) -> dict:
    """Get a sprite's .gfx file and texture path."""
    rec = gfx_index.resolve(name)
    if rec is None:
        return {"ok": False, "name": name, "error": "Sprite not found in mod or vanilla"}
    return {
        "ok": True,
        "name": rec["name"],
        "kind": rec.get("kind"),
        "texturefile": rec.get("texturefile"),
        "file": rec["file"],
        "line": rec["line"],
    }


def resolve_event_tool(event_id: str, settings: Settings, event_index: EventIndex) -> dict:
    """Get an event's file/line + namespace context (for namespace-mismatch debugging)."""
    rec = event_index.resolve(event_id)
    if rec is None:
        return {"ok": False, "id": event_id, "error": "Event not found"}
    return {
        "ok": True,
        "id": rec["id"],
        "kind": rec.get("kind"),
        "namespace": rec.get("namespace"),
        "file": rec["file"],
        "line": rec["line"],
        "file_namespaces": rec.get("file_namespaces", []),
    }


def resolve_decision_tool(
    decision_id: str, settings: Settings, decision_index: DecisionIndex
) -> dict:
    """Get a decision's file/line + category."""
    rec = decision_index.resolve(decision_id)
    if rec is None:
        return {"ok": False, "id": decision_id, "error": "Decision not found"}
    return {
        "ok": True,
        "id": rec["id"],
        "category": rec.get("category"),
        "file": rec["file"],
        "line": rec["line"],
    }


def resolve_idea_tool(idea_id: str, settings: Settings, idea_index: IdeaIndex) -> dict:
    """Get an idea's file/line + category + slot."""
    rec = idea_index.resolve(idea_id)
    if rec is None:
        return {"ok": False, "id": idea_id, "error": "Idea not found"}
    return {
        "ok": True,
        "id": rec["id"],
        "category": rec.get("category"),
        "slot": rec.get("slot"),
        "file": rec["file"],
        "line": rec["line"],
    }
