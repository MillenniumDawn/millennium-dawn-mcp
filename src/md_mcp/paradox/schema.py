"""Schema projections — extract typed information from a parsed AST.

The TS `schema.ts` exposes a full `convertNodeToJson(node, schemaDef)` system. This
module covers the subset the MCP server needs:

  * `to_json(node)` — convert any Node to a JSON-serialisable dict (used by `parse_file`
    and `parse_string` MCP tools)
  * `extract_focus_ids(root)` — port of `extractFocusIds` from `previewdef/focustree/schema.ts`
  * Extractors for events, decisions, ideas, and sprites, plus the `EVENT_KINDS` /
    `SPRITE_KINDS` container-kind lists that `indexes/` reuses to stay in sync.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

from ..util.line_numbers import line_starts, pos_to_line
from .nodes import Node, SymbolNode


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
    return _node_to_json(node, None)


def to_json_with_lines(node: Node, source: str) -> dict:
    """Same as to_json, but resolves line numbers from the source text.

    Used by parse_file/parse_string MCP tools so the agent can navigate directly.
    """
    return _node_to_json(node, line_starts(source))


def _node_to_json(node: Node, starts: Optional[list[int]]) -> dict:
    """Serialise one node. `starts=None` means no source text, so every `line` is null."""
    return {
        "name": node.name,
        "operator": node.operator,
        "value": _value_to_json(node.value, starts),
        "value_attachment": node.value_attachment.name if node.value_attachment else None,
        "line": (
            pos_to_line(node.name_token.start, starts) if node.name_token and starts else None
        ),
    }


def _value_to_json(value: Any, starts: Optional[list[int]]) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, SymbolNode):
        return {"kind": "symbol", "name": value.name}
    if isinstance(value, list):
        return {"kind": "block", "children": [_node_to_json(c, starts) for c in value]}
    raise TypeError(f"Unrepresentable value of type {type(value).__name__}")


# ---------------------------------------------------------------------------
# Focus extractors — port of `previewdef/focustree/schema.ts::extractFocusIds`.
# ---------------------------------------------------------------------------


def is_focus_file_content(text: str) -> bool:
    """Cheap pre-filter mirroring sharedFocusIndex.ts behaviour."""
    return "focus_tree" in text or "shared_focus" in text or "joint_focus" in text


def extract_focus_ids(root: Node) -> list[str]:
    """Return every focus ID defined in a parsed focus file.

    Handles all three forms:
        focus_tree = { ... focus = { id = X ... } ... }
        shared_focus = { id = X ... }
        joint_focus = { id = X ... }
    """
    ids: list[str] = []

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


def extract_focus_records(root: Node, source: str | None = None) -> list[dict]:
    """Return every focus with its location and parsed metadata.

    Each record has: `id`, `line` (1-based, or None if source not supplied), `kind`
    (`focus_tree` | `shared_focus` | `joint_focus`), `x`, `y`, `cost`, `icon`,
    `prerequisites: list[list[str]]`, `mutually_exclusive: list[str]`.
    """
    starts = _starts(source)
    records: list[dict] = []

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

    prereqs: list[list[str]] = []
    for p in node.get_all("prerequisite"):
        group: list[str] = []
        for child in p.children():
            if child.name == "focus" and isinstance(child.value, SymbolNode):
                group.append(child.value.name)
        if group:
            prereqs.append(group)

    mutex_node = node.get("mutually_exclusive")
    mutex: list[str] = []
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


# Canonical list of event container kinds — indexes/event.py reuses this for its
# text prefilter so a new kind only needs to be added here.
EVENT_KINDS = (
    "country_event",
    "news_event",
    "state_event",
    "unit_leader_event",
    "operative_leader_event",
)


def _iter_event_definitions(root: Node) -> Iterator[tuple[Node, str]]:
    """Yield `(node, id_str)` for every node satisfying the event hierarchy."""
    for top in root.children():
        if top.name in EVENT_KINDS:
            id_str = _get_id(top)
            if id_str:
                yield top, id_str


def _file_namespaces(root: Node) -> list[str]:
    """Return every `add_namespace = X` declaration at the top level of a file."""
    namespaces: list[str] = []
    for top in root.children():
        if top.name == "add_namespace":
            ns_val = top.value
            if isinstance(ns_val, SymbolNode):
                namespaces.append(ns_val.name)
            elif isinstance(ns_val, str):
                namespaces.append(ns_val)
    return namespaces


def extract_event_records(root: Node, source: str | None = None) -> list[dict]:
    """Return every event definition: {id, kind, namespace, file_namespaces, line}.

    `namespace` is parsed from the event id (`Afghanistan.3` → `Afghanistan`).
    `file_namespaces` is the list of `add_namespace = X` declarations from the same file —
    helpful for cross-checking the validator's "namespace mismatch" rule.
    """
    starts = _starts(source)
    namespaces = _file_namespaces(root)
    return [
        {
            "id": id_str,
            "kind": node.name,
            "namespace": id_str.partition(".")[0],
            "line": (
                pos_to_line(node.name_token.start, starts)
                if starts is not None and node.name_token
                else None
            ),
            "file_namespaces": list(namespaces),
        }
        for node, id_str in _iter_event_definitions(root)
    ]


def find_event_nodes(root: Node, event_id: str) -> list[Node]:
    """Return every node in the AST satisfying the event hierarchy for `event_id`.

    Mirrors `extract_event_records`'s walk so resource handlers can anchor to the
    same nodes the index was built from, instead of matching on name alone.
    """
    return [node for node, id_str in _iter_event_definitions(root) if id_str == event_id]


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


def _iter_decision_definitions(root: Node) -> Iterator[tuple[Node, str]]:
    """Yield `(node, category)` for every node satisfying the decision hierarchy.

    HOI4 decision files have the structure:

        category_name = {
            decision_id = { ... }
            decision_id_2 = { ... }
            # plus optional category-level keywords (icon, picture, priority, ...)
        }
    """
    for top in root.children():
        if top.name is None or not isinstance(top.value, list):
            continue
        category = top.name

        for child in top.children():
            if child.name in _DECISION_CATEGORY_KEYWORDS:
                continue
            if not isinstance(child.value, list):
                continue
            yield child, category


def extract_decision_records(root: Node, source: str | None = None) -> list[dict]:
    """Return every decision definition: {id, category, line}."""
    starts = _starts(source)
    return [
        {
            "id": node.name,
            "category": category,
            "line": (
                pos_to_line(node.name_token.start, starts)
                if starts is not None and node.name_token
                else None
            ),
        }
        for node, category in _iter_decision_definitions(root)
    ]


def find_decision_nodes(root: Node, decision_id: str) -> list[Node]:
    """Return every node in the AST satisfying the decision hierarchy for `decision_id`.

    Mirrors `extract_decision_records`'s walk so resource handlers can anchor to the
    same nodes the index was built from, instead of matching on name alone.
    """
    return [
        node for node, _category in _iter_decision_definitions(root) if node.name == decision_id
    ]


# Idea slots are declared inside categories with these keywords as immediate children;
# they're not ideas themselves.
_IDEA_SLOT_KEYWORDS = frozenset(
    {
        "law",
        "use_list_view",
        "designer",
    }
)


def _iter_idea_definitions(root: Node) -> Iterator[tuple[Node, str, Optional[str]]]:
    """Yield `(node, category, slot)` for every node satisfying the idea hierarchy.

    Walks `ideas = { category = { idea_id = { ... } } }` structures. Skips
    category-level config (`law = yes`, `use_list_view = yes`) and slot-only nodes.
    A leaf is recognised as an idea when its value is a block (has children).
    """
    ideas_root = next((c for c in root.children() if c.name == "ideas"), None)
    if ideas_root is None:
        return

    for category in ideas_root.children():
        if category.name is None or not isinstance(category.value, list):
            continue
        # _iter_idea_category is a generator, so this yield-from is valid.
        # pi-lens-ignore: no-yield-from-non-iterable
        yield from _iter_idea_category(category, category.name)


def _iter_idea_category(category: Node, cat_name: str) -> Iterator[tuple[Node, str, Optional[str]]]:
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
                yield grandchild, cat_name, child.name
            continue

        yield child, cat_name, None


def extract_idea_records(root: Node, source: str | None = None) -> list[dict]:
    """Return every idea definition: {id, category, slot, line}."""
    starts = _starts(source)
    return [
        {
            "id": node.name,
            "category": category,
            "slot": slot,
            "line": (
                pos_to_line(node.name_token.start, starts)
                if starts is not None and node.name_token
                else None
            ),
        }
        for node, category, slot in _iter_idea_definitions(root)
    ]


def find_idea_nodes(root: Node, idea_id: str) -> list[Node]:
    """Return every node in the AST satisfying the idea hierarchy for `idea_id`.

    Mirrors `extract_idea_records`'s walk so resource handlers can anchor to the
    same nodes the index was built from, instead of matching on name alone.
    """
    return [node for node, _category, _slot in _iter_idea_definitions(root) if node.name == idea_id]


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
    """True if `node` is a slot containing more idea blocks, not an idea itself.

    A slot wrapper holds further idea blocks and none of its own direct children
    are idea properties. Two cases the old all()-only test got wrong:

    An empty block (``X = {}``) made all() vacuously true and was skipped as a
    slot, so the idea vanished from the index. An empty block is an idea.

    An idea whose keys are all absent from `_IDEA_PROPERTIES` (scalar values, no
    block-valued children) also passed all() and was treated as a slot, so its
    scalar children were indexed as ideas and the idea itself disappeared. A slot
    wrapper must have at least one block-valued child.
    """
    children = list(node.children())
    if not children:
        return False
    if any(c.name in _IDEA_PROPERTIES for c in children):
        return False
    return any(isinstance(c.value, list) for c in children)


# ---------------------------------------------------------------------------
# GFX extractors — port of `previewdef/gfx` + `hoiformat/spritetype.ts`.
# ---------------------------------------------------------------------------


# Canonical list of sprite container kinds — indexes/gfx.py reuses this to build
# its regex scanner, so order must stay deterministic (a plain tuple, not a set).
SPRITE_KINDS = (
    "spriteType",
    "corneredTileSpriteType",
    "frameAnimatedSpriteType",
    "maskedShieldType",
    "progressbartype",
    "barChartType",
    "PieChartType",
    "LineChartType",
    "scrollingSprite",
)


def _iter_sprite_definitions(root: Node) -> Iterator[tuple[Node, str, str]]:
    """Yield `(node, name, parent)` for every node satisfying the sprite hierarchy.

    Walks any `spriteTypes = { ... }` (or `spriteTypes_<x>`) block and pulls
    `spriteType = { name = "GFX_x" texturefile = "..." }` style entries (plus the
    other sprite-kind variants enumerated above).
    """
    for top in root.children():
        if not (top.name and top.name.lower().startswith("spritetypes")):
            continue
        for sprite in top.children():
            if sprite.name not in SPRITE_KINDS:
                continue
            name_node = sprite.get("name")
            if name_node is None:
                continue
            name_val = name_node.value
            if isinstance(name_val, SymbolNode):
                name = name_val.name
            elif isinstance(name_val, str):
                name = name_val
            else:
                continue
            yield sprite, name, top.name


def extract_sprite_records(root: Node, source: str | None = None) -> list[dict]:
    """Return every sprite definition: {name, kind, texturefile, line, parent}."""
    starts = _starts(source)
    return [
        {
            "name": name,
            "kind": node.name,
            "texturefile": _scalar(node.get("texturefile")),
            "parent": parent,
            "line": (
                pos_to_line(node.name_token.start, starts)
                if starts is not None and node.name_token
                else None
            ),
        }
        for node, name, parent in _iter_sprite_definitions(root)
    ]


def find_sprite_nodes(root: Node, name: str) -> list[Node]:
    """Return every node in the AST satisfying the sprite hierarchy for `name`.

    Mirrors `extract_sprite_records`'s walk so resource handlers can anchor to the
    same nodes the index was built from, instead of matching on name alone.
    """
    return [
        node for node, sprite_name, _parent in _iter_sprite_definitions(root) if sprite_name == name
    ]


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
