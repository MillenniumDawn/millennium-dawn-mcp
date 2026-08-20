"""Schema projections — extract typed information from a parsed AST.

The TS `schema.ts` exposes a full `convertNodeToJson(node, schemaDef)` system. For
Milestone 1, we only need:

  * `to_json(node)` — convert any Node to a JSON-serialisable dict (used by `parse_file`
    and `parse_string` MCP tools)
  * `extract_focus_ids(root)` — port of `extractFocusIds` from `previewdef/focustree/schema.ts`

Additional extractors (events, decisions, ideas, sprites) land in Milestone 2.
"""

from __future__ import annotations

from typing import Any, List, Optional

from md_mcp.paradox.nodes import Node, SymbolNode

# pi-lens-ignore: reportMissingImports
from md_mcp.util.line_numbers import line_starts, pos_to_line


def _starts(source: str | None) -> list[int] | None:
    return line_starts(source) if source else None


def to_json(node: Node) -> dict:
    """Convert a Node to a JSON-friendly dict.

    Tagged-union representation chosen over TS's overloaded `NodeValue` union — strictly
    simpler when serialised to the agent. Shape:

        {
            "name": str | null,
            "operator": str | null,
            "value": JsonValue,
            "value_attachment": str | null,
            "line": int | null,    # 1-based, from name_token position
        }

    Where `JsonValue` is one of:
        * null
        * scalar string / number / bool
        * {"kind": "symbol", "name": "..."}
        * {"kind": "block", "children": [Node, ...]}
    """
    return {
        "name": node.name,
        "operator": node.operator,
        "value": _value_to_json(node.value),
        "value_attachment": node.value_attachment.name if node.value_attachment else None,
        "line": _token_line(node.name_token.start) if node.name_token else None,
    }


def _value_to_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, SymbolNode):
        return {"kind": "symbol", "name": value.name}
    if isinstance(value, list):
        return {"kind": "block", "children": [to_json(c) for c in value]}
    raise TypeError(f"Unrepresentable value of type {type(value).__name__}")


# Line numbers are computed on demand in to_json; we don't have the source text here,
# so callers wanting accurate lines must supply them externally. Default to None.
def _token_line(_start: int) -> Optional[int]:
    return None


def to_json_with_lines(node: Node, source: str) -> dict:
    """Same as to_json, but resolves line numbers from the source text.

    Used by parse_file/parse_string MCP tools so the agent can navigate directly.
    """
    starts = line_starts(source)
    return _to_json_with_lines(node, starts)


def _to_json_with_lines(node: Node, starts: list[int]) -> dict:
    return {
        "name": node.name,
        "operator": node.operator,
        "value": _value_to_json_with_lines(node.value, starts),
        "value_attachment": node.value_attachment.name if node.value_attachment else None,
        "line": pos_to_line(node.name_token.start, starts) if node.name_token else None,
    }


def _value_to_json_with_lines(value: Any, starts: list[int]) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, SymbolNode):
        return {"kind": "symbol", "name": value.name}
    if isinstance(value, list):
        return {"kind": "block", "children": [_to_json_with_lines(c, starts) for c in value]}
    raise TypeError(f"Unrepresentable value of type {type(value).__name__}")


# ---------------------------------------------------------------------------
# Focus extractors — port of `previewdef/focustree/schema.ts::extractFocusIds`.
# ---------------------------------------------------------------------------


def is_focus_file_content(text: str) -> bool:
    """Cheap pre-filter mirroring sharedFocusIndex.ts behaviour."""
    return "focus_tree" in text or "shared_focus" in text or "joint_focus" in text


def extract_focus_ids(root: Node) -> List[str]:
    """Return every focus ID defined in a parsed focus file.

    Handles all three forms:
        focus_tree = { ... focus = { id = X ... } ... }
        shared_focus = { id = X ... }
        joint_focus = { id = X ... }
    """
    ids: List[str] = []

    for top in root.children():
        if top.name == "focus_tree":
            for sub in top.children():
                if sub.name == "focus":
                    fid = _get_id(sub)
                    if fid:
                        ids.append(fid)
        elif top.name in ("shared_focus", "joint_focus"):
            fid = _get_id(top)
            if fid:
                ids.append(fid)

    return ids


def extract_focus_records(root: Node, source: str | None = None) -> List[dict]:
    """Return every focus with its location and parsed metadata.

    Each record has: `id`, `line` (1-based, or None if source not supplied), `kind`
    (`focus_tree` | `shared_focus` | `joint_focus`), `x`, `y`, `cost`, `icon`,
    `prerequisites: list[list[str]]`, `mutually_exclusive: list[str]`.
    """
    starts = _starts(source)
    records: List[dict] = []

    for top in root.children():
        if top.name == "focus_tree":
            for sub in top.children():
                if sub.name == "focus":
                    rec = _focus_record(sub, "focus_tree", starts)
                    if rec:
                        records.append(rec)
        elif top.name == "shared_focus":
            rec = _focus_record(top, "shared_focus", starts)
            if rec:
                records.append(rec)
        elif top.name == "joint_focus":
            rec = _focus_record(top, "joint_focus", starts)
            if rec:
                records.append(rec)

    return records


def _focus_record(node: Node, kind: str, starts: list[int] | None) -> dict | None:
    fid = _get_id(node)
    if not fid:
        return None

    prereqs: List[List[str]] = []
    for p in node.get_all("prerequisite"):
        group: List[str] = []
        for child in p.children():
            if child.name == "focus" and isinstance(child.value, SymbolNode):
                group.append(child.value.name)
        if group:
            prereqs.append(group)

    mutex_node = node.get("mutually_exclusive")
    mutex: List[str] = []
    if mutex_node:
        for child in mutex_node.children():
            if child.name == "focus" and isinstance(child.value, SymbolNode):
                mutex.append(child.value.name)

    return {
        "id": fid,
        "kind": kind,
        "line": (
            pos_to_line(node.name_token.start, starts)
            if starts is not None and node.name_token
            else None
        ),
        "x": _scalar(node.get("x")),
        "y": _scalar(node.get("y")),
        "cost": _scalar(node.get("cost")),
        "icon": _scalar(node.get("icon")),
        "prerequisites": prereqs,
        "mutually_exclusive": mutex,
        "relative_position_id": _scalar(node.get("relative_position_id")),
        "ai_will_do": _ai_will_do_summary(node),
    }


def _ai_will_do_summary(node: Node) -> dict | None:
    """Compact `ai_will_do` projection: base/factor scalars + modifier count."""
    awd = node.get("ai_will_do")
    if awd is None:
        return None
    out: dict = {"modifiers": 0}
    for c in awd.children():
        if c.name in ("base", "factor"):
            v = _scalar(c)
            if v is not None:
                out[c.name] = v
        elif c.name == "modifier":
            out["modifiers"] += 1
    return out


def _get_id(node: Node) -> str | None:
    """Pull the `id = X` value as a string; X is typically a SymbolNode but may be a string."""
    id_node = node.get("id")
    if id_node is None:
        return None
    v = id_node.value
    if isinstance(v, SymbolNode):
        return v.name
    if isinstance(v, str):
        return v
    return None


# ---------------------------------------------------------------------------
# Event / decision / idea extractors.
# ---------------------------------------------------------------------------


_EVENT_KINDS = frozenset(
    {"country_event", "news_event", "state_event", "unit_leader_event", "operative_leader_event"}
)


def extract_event_records(root: Node, source: str | None = None) -> List[dict]:
    """Return every event definition: {id, kind, namespace, file_namespaces, line}.

    `namespace` is parsed from the event id (`Afghanistan.3` → `Afghanistan`).
    `file_namespaces` is the list of `add_namespace = X` declarations from the same file —
    helpful for cross-checking the validator's "namespace mismatch" rule.
    """
    starts = _starts(source)
    file_namespaces: List[str] = []
    records: List[dict] = []

    for top in root.children():
        if top.name == "add_namespace":
            ns_val = top.value
            if isinstance(ns_val, SymbolNode):
                file_namespaces.append(ns_val.name)
            elif isinstance(ns_val, str):
                file_namespaces.append(ns_val)
            continue

        if top.name in _EVENT_KINDS:
            id_str = _get_id(top)
            if not id_str:
                continue
            ns, _, _ = id_str.partition(".")
            records.append(
                {
                    "id": id_str,
                    "kind": top.name,
                    "namespace": ns,
                    "line": (
                        pos_to_line(top.name_token.start, starts)
                        if starts is not None and top.name_token
                        else None
                    ),
                }
            )

    # Attach file_namespaces post-hoc so each record carries the file context.
    for r in records:
        r["file_namespaces"] = list(file_namespaces)

    return records


# Common keywords that appear inside a decision category but are NOT decisions
# themselves — they're config properties of the category.
_DECISION_CATEGORY_KEYWORDS = frozenset(
    {
        "icon",
        "picture",
        "priority",
        "allowed",
        "visible",
        "visibility_type",
        "available",
        "selectable_mission",
        "scripted_gui",
        "highlight_states",
        "highlight_state_targets",
        "highlight_provinces",
        "highlight_color_while_active",
        "on_map_area",
    }
)


def extract_decision_records(root: Node, source: str | None = None) -> List[dict]:
    """Return every decision definition: {id, category, line}.

    HOI4 decision files have the structure:

        category_name = {
            decision_id = { ... }
            decision_id_2 = { ... }
            # plus optional category-level keywords (icon, picture, priority, ...)
        }
    """
    starts = _starts(source)
    records: List[dict] = []

    for top in root.children():
        if top.name is None or not isinstance(top.value, list):
            continue
        category = top.name

        for child in top.children():
            if child.name in _DECISION_CATEGORY_KEYWORDS:
                continue
            if not isinstance(child.value, list):
                continue
            records.append(
                {
                    "id": child.name,
                    "category": category,
                    "line": (
                        pos_to_line(child.name_token.start, starts)
                        if starts is not None and child.name_token
                        else None
                    ),
                }
            )

    return records


# Idea slots are declared inside categories with these keywords as immediate children;
# they're not ideas themselves.
_IDEA_SLOT_KEYWORDS = frozenset(
    {
        "law",
        "use_list_view",
        "designer",
    }
)


def extract_idea_records(root: Node, source: str | None = None) -> List[dict]:
    """Return every idea definition: {id, category, line}.

    Walks `ideas = { category = { idea_id = { ... } } }` structures. Skips
    category-level config (`law = yes`, `use_list_view = yes`) and slot-only nodes.
    A leaf is recognised as an idea when its value is a block (has children).
    """
    starts = _starts(source)
    records: List[dict] = []

    ideas_root = next((c for c in root.children() if c.name == "ideas"), None)
    if ideas_root is None:
        return records

    for category in ideas_root.children():
        if category.name is None or not isinstance(category.value, list):
            continue
        cat_name = category.name
        _walk_idea_category(category, cat_name, records, starts)

    return records


def _walk_idea_category(
    category: Node,
    cat_name: str,
    out: List[dict],
    starts: list[int] | None,
) -> None:
    for child in category.children():
        if child.name in _IDEA_SLOT_KEYWORDS:
            continue
        if not isinstance(child.value, list):
            continue
        # An "idea" is any direct child with a block body. Some files have an extra
        # level for slots — detect that by checking if the child's children look like
        # ideas (all have block bodies and no idea-specific keys themselves).
        if _looks_like_slot_wrapper(child):
            for grandchild in child.children():
                if grandchild.name in _IDEA_SLOT_KEYWORDS:
                    continue
                if not isinstance(grandchild.value, list):
                    continue
                out.append(
                    {
                        "id": grandchild.name,
                        "category": cat_name,
                        "slot": child.name,
                        "line": (
                            pos_to_line(grandchild.name_token.start, starts)
                            if starts is not None and grandchild.name_token
                            else None
                        ),
                    }
                )
            continue

        out.append(
            {
                "id": child.name,
                "category": cat_name,
                "slot": None,
                "line": (
                    pos_to_line(child.name_token.start, starts)
                    if starts is not None and child.name_token
                    else None
                ),
            }
        )


# Properties that, when present as a direct child, identify a block as an idea
# (not a slot wrapper containing further ideas).
_IDEA_PROPERTIES = frozenset(
    {
        "modifier",
        "picture",
        "allowed",
        "available",
        "research_bonus",
        "equipment_bonus",
        "production_bonus",
        "traits",
        "ledger",
        "removal_cost",
        "cost",
        "rule",
        "targeted_modifier",
        "cancel",
        "level",
    }
)


def _looks_like_slot_wrapper(node: Node) -> bool:
    """True if `node` is a slot containing more idea blocks, not an idea itself."""
    return all(c.name not in _IDEA_PROPERTIES for c in node.children())


# ---------------------------------------------------------------------------
# GFX extractors — port of `previewdef/gfx` + `hoiformat/spritetype.ts`.
# ---------------------------------------------------------------------------


_SPRITE_KINDS = frozenset(
    {
        "spriteType",
        "corneredTileSpriteType",
        "frameAnimatedSpriteType",
        "maskedShieldType",
        "progressbartype",
        "barChartType",
        "PieChartType",
        "LineChartType",
        "scrollingSprite",
    }
)


def extract_sprite_records(root: Node, source: str | None = None) -> List[dict]:
    """Return every sprite definition: {name, kind, texturefile, line, parent_block}.

    Walks any `spriteTypes = { ... }` (or `spriteTypes_<x>`) block and pulls
    `spriteType = { name = "GFX_x" texturefile = "..." }` style entries (plus the
    other sprite-kind variants enumerated above).
    """
    starts = _starts(source)
    records: List[dict] = []
    for top in root.children():
        if top.name and top.name.lower().startswith("spritetypes"):
            for sprite in top.children():
                if sprite.name and sprite.name in _SPRITE_KINDS:
                    rec = _sprite_record(sprite, top.name, starts)
                    if rec:
                        records.append(rec)
    return records


def _sprite_record(node: Node, parent: str, starts: list[int] | None) -> dict | None:
    name_node = node.get("name")
    if name_node is None:
        return None
    name_val = name_node.value
    if isinstance(name_val, SymbolNode):
        name = name_val.name
    elif isinstance(name_val, str):
        name = name_val
    else:
        return None

    texture_val = _scalar(node.get("texturefile"))
    return {
        "name": name,
        "kind": node.name,
        "texturefile": texture_val,
        "parent": parent,
        "line": (
            pos_to_line(node.name_token.start, starts)
            if starts is not None and node.name_token
            else None
        ),
    }


def _scalar(node: Node | None) -> Any:
    """Unwrap a value node to a plain scalar (None, str, number, bool)."""
    if node is None:
        return None
    v = node.value
    if isinstance(v, SymbolNode):
        if v.name == "yes":
            return True
        if v.name == "no":
            return False
        return v.name
    if isinstance(v, (int, float, str)):
        return v
    return None
