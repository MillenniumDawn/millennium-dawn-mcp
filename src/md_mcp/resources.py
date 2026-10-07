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
from .util.line_numbers import line_starts, pos_to_line
from .util.pathing import resolve_scope_file


def focus_resource(focus_id: str, settings: Settings, focus_index: FocusIndex) -> str:
    """Return the raw script of a focus block, including surrounding braces."""
    cached = focus_index.resolve(focus_id)
    if cached is None:
        raise KeyError(f"Focus '{focus_id}' not found")
    abs_path = resolve_scope_file(
        cached["file"], settings.mod_root, settings.vanilla_path, settings.submod_root
    )
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {cached['file']}")

    text, root = parse_cached(abs_path)
    return _extract_focus_block(text, focus_id, root)


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
    node = _anchor(find_nodes(root, ident), text, rec, kind=kind, ident=ident)
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
    start = _slice_start(text, node.name_token.start)
    return text[start : node.value_end_token.end]


def _extract_focus_block(text: str, focus_id: str, root: Optional[Node] = None) -> str:
    """Find `focus = { id = <id> ... }` (or `shared_focus`/`joint_focus`) and return raw text.

    Uses the parser to locate the block by line, then slices the text by brace
    matching from there — this preserves comments and original whitespace, which
    parsing-then-rendering would strip.
    """
    if root is None:
        root = parse_string(text)

    candidates = find_focus_nodes(root, focus_id)
    if not candidates:
        raise KeyError(f"Focus '{focus_id}' resolved by index but not located in file")

    # Slice from `name_token` (plus any indentation before it) to the matching `}`.
    cand = candidates[0]
    if cand.name_token is None or cand.value_end_token is None:
        raise KeyError(f"Focus '{focus_id}' has no position information (malformed parse)")
    start = _slice_start(text, cand.name_token.start)
    end = cand.value_end_token.end
    return text[start:end]


def _slice_start(text: str, pos: int) -> int:
    """Start of the line when only indentation precedes `pos`, else `pos` itself."""
    line_start = text.rfind("\n", 0, pos) + 1
    return pos if text[line_start:pos].strip() else line_start
