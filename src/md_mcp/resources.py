"""MCP resources — raw text streamed via `md://` URIs.

Resources complement tools: tools return JSON; resources stream the raw paradox
script or localisation text directly into the agent's context. Useful for quoting,
extracting verbatim, or feeding back into Edit/Write.
"""

from __future__ import annotations

from pathlib import Path
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
from .util.encoding import read_text


def focus_resource(focus_id: str, settings: Settings, focus_index: FocusIndex) -> str:
    """Return the raw script of a focus block, including surrounding braces."""
    cached = focus_index.resolve(focus_id)
    if cached is None:
        raise KeyError(f"Focus '{focus_id}' not found")
    abs_path = _resolve(cached["file"], settings)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {cached['file']}")

    text = read_text(abs_path)
    return _extract_focus_block(text, focus_id)


def loc_resource(key: str, settings: Settings, loc_index: LocalisationIndex, lang: Optional[str] = None) -> str:
    """Return the value of a single loc key, falling back to English."""
    rec = loc_index.resolve(key, lang or settings.default_lang)
    if rec is None:
        raise KeyError(f"Loc key '{key}' not found")
    return rec["value"]


def sprite_resource(name: str, settings: Settings, gfx_index: GfxIndex) -> str:
    """Return the raw `spriteType = { ... }` block for the named sprite."""
    rec = gfx_index.resolve(name)
    if rec is None:
        raise KeyError(f"Sprite '{name}' not found")
    abs_path = _resolve(rec["file"], settings)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {rec['file']}")
    text = read_text(abs_path)
    return _extract_block_by_key(text, key="name", value=name, container_kinds={"spriteType", "corneredTileSpriteType", "frameAnimatedSpriteType"})


def event_resource(event_id: str, settings: Settings, event_index: EventIndex) -> str:
    """Return the raw event block for `<namespace>.<n>`."""
    rec = event_index.resolve(event_id)
    if rec is None:
        raise KeyError(f"Event '{event_id}' not found")
    abs_path = _resolve(rec["file"], settings)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {rec['file']}")
    text = read_text(abs_path)
    return _extract_block_by_key(
        text,
        key="id",
        value=event_id,
        container_kinds={"country_event", "news_event", "state_event", "unit_leader_event", "operative_leader_event"},
    )


def decision_resource(decision_id: str, settings: Settings, decision_index: DecisionIndex) -> str:
    """Return the raw `<decision_id> = { ... }` block."""
    rec = decision_index.resolve(decision_id)
    if rec is None:
        raise KeyError(f"Decision '{decision_id}' not found")
    abs_path = _resolve(rec["file"], settings)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {rec['file']}")
    text = read_text(abs_path)
    return _extract_named_block(text, decision_id)


def idea_resource(idea_id: str, settings: Settings, idea_index: IdeaIndex) -> str:
    """Return the raw `<idea_id> = { ... }` block."""
    rec = idea_index.resolve(idea_id)
    if rec is None:
        raise KeyError(f"Idea '{idea_id}' not found")
    abs_path = _resolve(rec["file"], settings)
    if abs_path is None:
        raise FileNotFoundError(f"Indexed file missing on disk: {rec['file']}")
    text = read_text(abs_path)
    return _extract_named_block(text, idea_id)


def _extract_block_by_key(text: str, *, key: str, value: str, container_kinds: set[str]) -> str:
    """Walk the AST for any `container = { key = value ... }` and return its source slice."""
    from .paradox.nodes import SymbolNode

    root = parse_string(text)

    def walk(nodes):
        for node in nodes:
            if node.name in container_kinds:
                target = node.get(key)
                if target is not None:
                    v = target.value
                    matches = (isinstance(v, SymbolNode) and v.name == value) or (
                        isinstance(v, str) and v == value
                    )
                    if matches and node.name_token and node.value_end_token:
                        start = _line_start(text, node.name_token.start)
                        return text[start : node.value_end_token.end]
            if isinstance(node.value, list):
                found = walk(node.value)
                if found is not None:
                    return found
        return None

    result = walk(root.children())
    if result is None:
        raise KeyError(f"{key}={value} not located in file")
    return result


def _extract_named_block(text: str, name: str) -> str:
    """Find any `name = { ... }` block and return its source slice (depth-first)."""
    root = parse_string(text)

    def walk(nodes):
        for node in nodes:
            if node.name == name and isinstance(node.value, list):
                if node.name_token and node.value_end_token:
                    start = _line_start(text, node.name_token.start)
                    return text[start : node.value_end_token.end]
            if isinstance(node.value, list):
                found = walk(node.value)
                if found is not None:
                    return found
        return None

    result = walk(root.children())
    if result is None:
        raise KeyError(f"Block '{name}' not located in file")
    return result


def _resolve(relpath: str, settings: Settings) -> Optional[Path]:
    p = settings.mod_root / relpath
    if p.exists():
        return p
    if settings.vanilla_path is not None:
        p = settings.vanilla_path / relpath
        if p.exists():
            return p
    return None


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
        from .paradox.nodes import SymbolNode

        v = id_node.value
        if isinstance(v, SymbolNode) and v.name == focus_id:
            pass
        elif isinstance(v, str) and v == focus_id:
            pass
        else:
            continue

        # Have the matching focus block — slice text from `cand.name_token.start`
        # back to the start of the line, forward to the matching `}`.
        if cand.name_token is None or cand.value_end_token is None:
            return ""  # malformed; bail
        start = _line_start(text, cand.name_token.start)
        end = cand.value_end_token.end
        return text[start:end]

    raise KeyError(f"Focus '{focus_id}' resolved by index but not located in file")


def _line_start(text: str, pos: int) -> int:
    nl = text.rfind("\n", 0, pos)
    return nl + 1 if nl >= 0 else 0
