"""MCP resources — raw text streamed via `md://` URIs.

Resources complement tools: tools return JSON; resources stream the raw paradox
script or localisation text directly into the agent's context. Useful for quoting,
extracting verbatim, or feeding back into Edit/Write.
"""

from __future__ import annotations

from typing import Callable, Optional

from .config import Settings
from .indexes import (
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GenericTxtIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
)
from .paradox import parse_string
from .paradox.ast_cache import parse_cached
from .paradox.nodes import Node
from .paradox.schema import (
    find_decision_nodes,
    find_event_nodes,
    find_focus_nodes,
    find_idea_nodes,
    find_sprite_nodes,
)
from .util.index_anchor import select_indexed_anchor
from .util.line_numbers import line_starts, pos_to_line
from .util.pathing import resolve_scope_file


def focus_resource(focus_id: str, settings: Settings, focus_index: FocusIndex) -> str:
    """Return the raw script of a focus block, including surrounding braces.

    Anchored to the index record's line so duplicates resolve to the same
    definition the index picked, not the first one in the file.
    """
    cached = focus_index.resolve(focus_id)
    if cached is None:
        raise KeyError(f"Focus '{focus_id}' not found")
    abs_path = resolve_scope_file(
        cached["file"], settings.mod_root, settings.vanilla_path, settings.submod_root
    )
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {cached['file']}")

    text, root = parse_cached(abs_path)
    return _extract_focus_block(text, focus_id, root, line=cached.get("line"))


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
    return _definition_resource(gfx_index, name, settings, find_sprite_nodes, kind="Sprite")


def event_resource(event_id: str, settings: Settings, event_index: EventIndex) -> str:
    """Return the raw event block for `<namespace>.<n>`, anchored to the indexed definition."""
    return _definition_resource(event_index, event_id, settings, find_event_nodes, kind="Event")


def decision_resource(decision_id: str, settings: Settings, decision_index: DecisionIndex) -> str:
    """Return the raw `<decision_id> = { ... }` block, anchored to the indexed definition."""
    return _definition_resource(
        decision_index, decision_id, settings, find_decision_nodes, kind="Decision"
    )


def idea_resource(idea_id: str, settings: Settings, idea_index: IdeaIndex) -> str:
    """Return the raw `<idea_id> = { ... }` block, anchored to the indexed definition."""
    return _definition_resource(idea_index, idea_id, settings, find_idea_nodes, kind="Idea")


def _definition_resource(
    index: GenericTxtIndex,
    ident: str,
    settings: Settings,
    find_nodes: Callable[[Node, str], list[Node]],
    *,
    kind: str,
) -> str:
    rec = index.resolve(ident)
    if rec is None:
        raise KeyError(f"{kind} '{ident}' not found")
    abs_path = resolve_scope_file(
        rec["file"], settings.mod_root, settings.vanilla_path, settings.submod_root
    )
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {rec['file']}")
    text, root = parse_cached(abs_path)
    node = _anchor(find_nodes(root, ident), text, rec.get("line"), kind=kind, ident=ident)
    return _slice_node(text, node)


def _anchor(
    candidates: list[Node], text: str, line: Optional[int], *, kind: str, ident: str
) -> Node:
    """Pick the single node the index record refers to, or fail clearly.

    Anchors by the record's indexed line when exactly one candidate starts there;
    a lone hierarchy-valid match is accepted when the index has no line, and every
    other case (stale line, several matches on the indexed line, no line to
    disambiguate several matches) is rejected as a clear `IndexAnchorError` rather
    than silently returning the first one.
    """
    starts = line_starts(text)
    entries = [
        (pos_to_line(node.name_token.start, starts) if node.name_token else None, node)
        for node in candidates
    ]
    return select_indexed_anchor(entries, line=line, kind=kind, ident=ident)


def _slice_node(text: str, node: Node) -> str:
    """Slice the exact source text for a definition node, comments and whitespace included."""
    if node.name_token is None or node.value_end_token is None:
        raise KeyError("Definition node has no position information (malformed parse)")
    start = _slice_start(text, node.name_token.start)
    return text[start : node.value_end_token.end]


def _extract_focus_block(
    text: str, focus_id: str, root: Optional[Node] = None, *, line: Optional[int] = None
) -> str:
    """Find `focus = { id = <id> ... }` (or `shared_focus`/`joint_focus`) and return raw text.

    Uses the parser to locate the block by line, then slices the text by brace
    matching from there — this preserves comments and original whitespace, which
    parsing-then-rendering would strip. `line` is the index record's line; the
    block is anchored to it so duplicate ids resolve to the same definition the
    index picked.
    """
    if root is None:
        root = parse_string(text)

    cand = _anchor(find_focus_nodes(root, focus_id), text, line, kind="Focus", ident=focus_id)

    # Slice from `name_token` (plus any indentation before it) to the matching `}`.
    if cand.name_token is None or cand.value_end_token is None:
        raise KeyError(f"Focus '{focus_id}' has no position information (malformed parse)")
    start = _slice_start(text, cand.name_token.start)
    end = cand.value_end_token.end
    return text[start:end]


def _slice_start(text: str, pos: int) -> int:
    """Start of the line when only indentation precedes `pos`, else `pos` itself."""
    line_start = text.rfind("\n", 0, pos) + 1
    return pos if text[line_start:pos].strip() else line_start
