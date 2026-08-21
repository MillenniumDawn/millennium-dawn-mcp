"""MCP resources — raw text streamed via `md://` URIs.

Resources complement tools: tools return JSON; resources stream the raw paradox
script or localisation text directly into the agent's context. Useful for quoting,
extracting verbatim, or feeding back into Edit/Write.
"""

from __future__ import annotations

from typing import Optional

from .config import Settings
from .indexes import (
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
)
from .paradox import parse_string
from .paradox.nodes import Node, SymbolNode
from .paradox.schema import (
    find_decision_nodes,
    find_event_nodes,
    find_idea_nodes,
    find_sprite_nodes,
)
from .util.encoding import read_text
from .util.line_numbers import line_starts, pos_to_line
from .util.pathing import resolve_scope_file


def focus_resource(focus_id: str, settings: Settings, focus_index: FocusIndex) -> str:
    """Return the raw script of a focus block, including surrounding braces."""
    cached = focus_index.resolve(focus_id)
    if cached is None:
        raise KeyError(f"Focus '{focus_id}' not found")
    abs_path = resolve_scope_file(cached["file"], settings.mod_root, settings.vanilla_path)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {cached['file']}")

    text = read_text(abs_path)
    return _extract_focus_block(text, focus_id)


def loc_resource(
    key: str, settings: Settings, loc_index: LocalisationIndex, lang: Optional[str] = None
) -> str:
    """Return the value of a single loc key, falling back to English."""
    rec = loc_index.resolve(key, lang or settings.default_lang)
    if rec is None:
        raise KeyError(f"Loc key '{key}' not found")
    return rec["value"]


def sprite_resource(name: str, settings: Settings, gfx_index: GfxIndex) -> str:
    """Return the raw `spriteType = { ... }` block for the named sprite, anchored to the index."""
    rec = gfx_index.resolve(name)
    if rec is None:
        raise KeyError(f"Sprite '{name}' not found")
    abs_path = resolve_scope_file(rec["file"], settings.mod_root, settings.vanilla_path)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {rec['file']}")
    text = read_text(abs_path)
    root = parse_string(text)
    candidates = find_sprite_nodes(root, name)
    node = _anchor(candidates, text, rec, kind="Sprite", ident=name)
    return _slice_node(text, node)


def event_resource(event_id: str, settings: Settings, event_index: EventIndex) -> str:
    """Return the raw event block for `<namespace>.<n>`, anchored to the indexed definition."""
    rec = event_index.resolve(event_id)
    if rec is None:
        raise KeyError(f"Event '{event_id}' not found")
    abs_path = resolve_scope_file(rec["file"], settings.mod_root, settings.vanilla_path)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {rec['file']}")
    text = read_text(abs_path)
    root = parse_string(text)
    candidates = find_event_nodes(root, event_id)
    node = _anchor(candidates, text, rec, kind="Event", ident=event_id)
    return _slice_node(text, node)


def decision_resource(decision_id: str, settings: Settings, decision_index: DecisionIndex) -> str:
    """Return the raw `<decision_id> = { ... }` block, anchored to the indexed definition."""
    rec = decision_index.resolve(decision_id)
    if rec is None:
        raise KeyError(f"Decision '{decision_id}' not found")
    abs_path = resolve_scope_file(rec["file"], settings.mod_root, settings.vanilla_path)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {rec['file']}")
    text = read_text(abs_path)
    root = parse_string(text)
    candidates = find_decision_nodes(root, decision_id)
    node = _anchor(candidates, text, rec, kind="Decision", ident=decision_id)
    return _slice_node(text, node)


def idea_resource(idea_id: str, settings: Settings, idea_index: IdeaIndex) -> str:
    """Return the raw `<idea_id> = { ... }` block, anchored to the indexed definition."""
    rec = idea_index.resolve(idea_id)
    if rec is None:
        raise KeyError(f"Idea '{idea_id}' not found")
    abs_path = resolve_scope_file(rec["file"], settings.mod_root, settings.vanilla_path)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {rec['file']}")
    text = read_text(abs_path)
    root = parse_string(text)
    candidates = find_idea_nodes(root, idea_id)
    node = _anchor(candidates, text, rec, kind="Idea", ident=idea_id)
    return _slice_node(text, node)


def _anchor(candidates: list[Node], text: str, rec: dict, *, kind: str, ident: str) -> Node:
    """Pick the single node the index record refers to, or fail clearly.

    Anchors by the record's indexed line when available; otherwise a lone
    hierarchy-valid match is accepted, but multiple matches are rejected as
    ambiguous rather than silently returning the first one.
    """
    if not candidates:
        raise KeyError(f"{kind} '{ident}' resolved by index but not located in file")

    line = rec.get("line")
    if line is not None:
        starts = line_starts(text)
        on_line = [
            n
            for n in candidates
            if n.name_token and pos_to_line(n.name_token.start, starts) == line
        ]
        if not on_line:
            raise KeyError(
                f"{kind} '{ident}' index points at line {line} but no matching definition sits "
                "there; the index is stale — delete <mod_root>/.md-mcp-cache/ and rerun "
                "`md-mcp build-index`"
            )
        return on_line[0]

    if len(candidates) > 1:
        raise KeyError(
            f"{kind} '{ident}' is ambiguous: {len(candidates)} definitions found and the index "
            "has no line to disambiguate"
        )
    return candidates[0]


def _slice_node(text: str, node: Node) -> str:
    """Slice the exact source text for a definition node, comments and whitespace included."""
    if node.name_token is None or node.value_end_token is None:
        raise KeyError("Definition node has no position information (malformed parse)")
    start = _line_start(text, node.name_token.start)
    return text[start : node.value_end_token.end]


def _extract_focus_block(text: str, focus_id: str) -> str:
    """Find `focus = { id = <id> ... }` (or `shared_focus`/`joint_focus`) and return raw text.

    Uses the parser to locate the block by line, then slices the text by brace
    matching from there — this preserves comments and original whitespace, which
    parsing-then-rendering would strip.
    """
    root = parse_string(text)

    candidates: list = []
    for top in root.children():
        if top.name == "focus_tree":
            for sub in top.children():
                if sub.name == "focus":
                    candidates.append(sub)
        elif top.name in ("shared_focus", "joint_focus"):
            candidates.append(top)

    for cand in candidates:
        id_node = cand.get("id")
        if id_node is None:
            continue
        v = id_node.value
        matches = (isinstance(v, SymbolNode) and v.name == focus_id) or (
            isinstance(v, str) and v == focus_id
        )
        if not matches:
            continue

        # Have the matching focus block — slice text from `cand.name_token.start`
        # back to the start of the line, forward to the matching `}`.
        if cand.name_token is None or cand.value_end_token is None:
            raise KeyError(f"Focus '{focus_id}' has no position information (malformed parse)")
        start = _line_start(text, cand.name_token.start)
        end = cand.value_end_token.end
        return text[start:end]

    raise KeyError(f"Focus '{focus_id}' resolved by index but not located in file")


def _line_start(text: str, pos: int) -> int:
    nl = text.rfind("\n", 0, pos)
    return nl + 1 if nl >= 0 else 0
