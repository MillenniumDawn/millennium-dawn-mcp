"""Scoped cross-reference audit — dangling ids across focus/event/idea/sprite/loc/decision.

Walks the AST of the files in scope, extracts every outbound reference the
indexes can resolve, and reports the ones that don't resolve. This is the
"audit this file and tell me what's broken" query: validators cover some of
this mod-wide with fixed rules, but they can't be scoped to a file, and
`resolve_*` answers one id at a time.

Reference kinds and where they're harvested:

  focus     — `prerequisite`/`mutually_exclusive` members, `relative_position_id`,
              `has_completed_focus`, `complete_national_focus`
  event     — `country_event` / `news_event` (symbol form or `{ id = ... }` block)
  idea      — `add_ideas` / `remove_ideas` (symbol or block), `add_idea` /
              `remove_idea` / `idea` / `has_idea`
  sprite    — `icon` / `picture` (tries the raw name, then `GFX_<name>`)
  loc       — `<focus_id>` and `<focus_id>_desc` for every focus defined in
              scope, plus `custom_effect_tooltip` keys
  decision  — `activate_decision`, `unlock_decision_tooltip`

Not checked (no index exists yet): country flags, variables, scripted effect
names. Reported in `not_checked` so absence of findings isn't mistaken for
coverage. If the vanilla install isn't configured, ids defined in vanilla
(ideas especially) will show as unresolved — `vanilla_indexed` flags this.
"""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from ..indexes import (
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
)
from ..paradox import parse_string
from ..paradox.nodes import Node, SymbolNode
from ..util.encoding import read_text
from ..util.pathing import resolve_scope_file
from ..util.response import enforce_budget

_ALL_KINDS: tuple = ("focus", "event", "idea", "sprite", "loc", "decision")
_MAX_FILES = 200

_EVENT_NODES = frozenset({"country_event", "news_event"})
_FOCUS_SYMBOL_NODES = frozenset(
    {"has_completed_focus", "complete_national_focus", "relative_position_id"}
)
_IDEA_BLOCK_NODES = frozenset({"add_ideas", "remove_ideas"})
_IDEA_SYMBOL_NODES = frozenset({"add_idea", "remove_idea", "idea", "has_idea"})
_SPRITE_NODES = frozenset({"icon", "picture"})
_LOC_NODES = frozenset({"custom_effect_tooltip"})
_DECISION_NODES = frozenset({"activate_decision", "unlock_decision_tooltip"})
_FOCUS_DEF_NODES = frozenset({"focus", "shared_focus", "joint_focus"})


def check_refs(
    mod_root: Path,
    *,
    focus_index: FocusIndex,
    event_index: EventIndex,
    idea_index: IdeaIndex,
    gfx_index: GfxIndex,
    loc_index: LocalisationIndex,
    decision_index: DecisionIndex,
    tag: Optional[str] = None,
    files: Optional[List[str]] = None,
    kinds: Optional[Sequence[str]] = None,
    vanilla_path: Optional[Path] = None,
    lang: str = "en",
    limit: int = 200,
    offset: int = 0,
    counts_only: bool = False,
) -> dict:
    """Audit cross-references in the given scope.

    Scope: `files=[...]` (mod-relative paths, any script type) or `tag=` (the
    tag's prefix-matched focus files; use `files=` to audit event/decision
    files). Unresolved refs are deduped by (kind, id) with an occurrence count
    and first sites.
    """
    if not tag and not files:
        return {"ok": False, "error": "Pass tag= or files=[...] (mod-relative paths)."}

    selected = list(kinds) if kinds else list(_ALL_KINDS)
    unknown = [k for k in selected if k not in _ALL_KINDS]
    if unknown:
        return {"ok": False, "error": f"Unknown kind(s): {unknown}. Valid: {list(_ALL_KINDS)}"}
    selected_set = set(selected)

    if files:
        scope_files = list(files)
    else:
        assert tag is not None
        scope_files = focus_index.files_for_tag(tag)

    files_truncated = len(scope_files) > _MAX_FILES
    scope_files = scope_files[:_MAX_FILES]

    # Collect raw references: (kind, ref, via, file, line, referrer).
    refs: List[dict] = []
    parse_errors: List[dict] = []
    focus_defs: List[dict] = []  # focus ids defined in scope, for loc coverage

    for relpath in scope_files:
        abs_path = resolve_scope_file(relpath, mod_root, vanilla_path)
        if abs_path is None:
            parse_errors.append({"file": relpath, "error": "not found"})
            continue
        try:
            text = read_text(abs_path)
            root = parse_string(text)
        except Exception as e:
            parse_errors.append({"file": relpath, "error": str(e)[:200]})
            continue
        line_starts = _line_starts(text)
        _walk(root, relpath, line_starts, selected_set, refs, focus_defs, referrer=None)

    if "loc" in selected_set:
        for fd in focus_defs:
            for key in (fd["id"], fd["id"] + "_desc"):
                refs.append(
                    {
                        "kind": "loc",
                        "ref": key,
                        "via": "focus_loc",
                        "file": fd["file"],
                        "line": fd["line"],
                        "referrer": fd["id"],
                    }
                )

    resolvers: Dict[str, Callable[[str], bool]] = {
        "focus": lambda r: focus_index.resolve(r) is not None,
        "event": lambda r: event_index.resolve(r) is not None,
        "idea": lambda r: idea_index.resolve(r) is not None,
        "sprite": lambda r: gfx_index.resolve(r) is not None
        or gfx_index.resolve(f"GFX_{r}") is not None,
        "loc": lambda r: loc_index.resolve(r, lang) is not None,
        "decision": lambda r: decision_index.resolve(r) is not None,
    }
    index_by_kind: Dict[str, Any] = {
        "focus": focus_index,
        "event": event_index,
        "idea": idea_index,
        "sprite": gfx_index,
        "loc": loc_index,
        "decision": decision_index,
    }
    for k in selected_set:
        index_by_kind[k].ensure_fresh()

    checked: Dict[str, Set[str]] = {k: set() for k in selected}
    unresolved_by_key: Dict[tuple, dict] = {}
    resolved_cache: Dict[tuple, bool] = {}

    for r in refs:
        kind, ref = r["kind"], r["ref"]
        checked[kind].add(ref)
        key = (kind, ref)
        ok = resolved_cache.get(key)
        if ok is None:
            ok = resolvers[kind](ref)
            resolved_cache[key] = ok
        if ok:
            continue
        entry = unresolved_by_key.get(key)
        if entry is None:
            entry = {
                "kind": kind,
                "ref": ref,
                "count": 0,
                "sites": [],
            }
            unresolved_by_key[key] = entry
        entry["count"] += 1
        if len(entry["sites"]) < 3:
            site = {"file": r["file"], "line": r["line"], "via": r["via"]}
            if r.get("referrer"):
                site["referrer"] = r["referrer"]
            entry["sites"].append(site)

    unresolved = sorted(unresolved_by_key.values(), key=lambda e: (e["kind"], e["ref"]))
    total = len(unresolved)
    sliced = unresolved[offset : offset + limit] if limit >= 0 else unresolved[offset:]

    result: dict = {
        "ok": True,
        "scope": {"tag": tag.upper()} if tag and not files else {"files": len(scope_files)},
        "files_scanned": len(scope_files),
        "files_truncated": files_truncated,
        "kinds_checked": selected,
        "not_checked": ["country_flags", "variables", "scripted_effects"],
        "vanilla_indexed": vanilla_path is not None,
        "counts": {
            k: {
                "checked": len(checked[k]),
                "unresolved": sum(1 for e in unresolved if e["kind"] == k),
            }
            for k in selected
        },
        "total_unresolved": total,
        "returned": len(sliced),
        "truncated": offset + len(sliced) < total,
    }
    if parse_errors:
        result["parse_errors"] = parse_errors
    if not counts_only:
        result["unresolved"] = sliced

    return enforce_budget(result, heavy_keys=("unresolved", "parse_errors"))


def _walk(
    node: Node,
    relpath: str,
    line_starts: List[int],
    kinds: Set[str],
    refs: List[dict],
    focus_defs: List[dict],
    referrer: Optional[str],
) -> None:
    for child in node.children():
        name = child.name
        ctx = referrer

        if name in _FOCUS_DEF_NODES:
            fid = _symbol_or_str(child_get(child, "id"))
            if fid:
                ctx = fid
                focus_defs.append({"id": fid, "file": relpath, "line": _line(child, line_starts)})

        if "focus" in kinds:
            if name in ("prerequisite", "mutually_exclusive"):
                for m in child.children():
                    if m.name == "focus":
                        ref = _symbol_or_str(m)
                        if ref:
                            refs.append(_ref("focus", ref, name, relpath, m, line_starts, ctx))
            elif name in _FOCUS_SYMBOL_NODES:
                ref = _symbol_or_str(child)
                if ref:
                    refs.append(_ref("focus", ref, name, relpath, child, line_starts, ctx))

        if "event" in kinds and name in _EVENT_NODES:
            ref = _symbol_or_str(child)
            if ref is None and isinstance(child.value, list):
                ref = _symbol_or_str(child_get(child, "id"))
            if ref:
                refs.append(_ref("event", ref, name, relpath, child, line_starts, ctx))

        if "idea" in kinds:
            if name in _IDEA_BLOCK_NODES:
                ref = _symbol_or_str(child)
                if ref:
                    refs.append(_ref("idea", ref, name, relpath, child, line_starts, ctx))
                elif isinstance(child.value, list):
                    for m in child.children():
                        # bare symbols inside the block parse as name-only nodes
                        if m.value is None and m.name:
                            refs.append(_ref("idea", m.name, name, relpath, m, line_starts, ctx))
            elif name in _IDEA_SYMBOL_NODES:
                ref = _symbol_or_str(child)
                if ref:
                    refs.append(_ref("idea", ref, name, relpath, child, line_starts, ctx))

        if "sprite" in kinds and name in _SPRITE_NODES:
            ref = _symbol_or_str(child)
            if ref is None and isinstance(child.value, list):
                for m in child.children():
                    if m.name == "value":
                        v = _symbol_or_str(m)
                        if v and not _is_texture_path(v):
                            refs.append(_ref("sprite", v, name, relpath, m, line_starts, ctx))
            elif ref and not _is_texture_path(ref):
                refs.append(_ref("sprite", ref, name, relpath, child, line_starts, ctx))

        if "loc" in kinds and name in _LOC_NODES:
            ref = _symbol_or_str(child)
            if ref:
                refs.append(_ref("loc", ref, name, relpath, child, line_starts, ctx))

        if "decision" in kinds and name in _DECISION_NODES:
            ref = _symbol_or_str(child)
            if ref:
                refs.append(_ref("decision", ref, name, relpath, child, line_starts, ctx))

        if isinstance(child.value, list):
            _walk(child, relpath, line_starts, kinds, refs, focus_defs, ctx)


def _ref(
    kind: str,
    ref: str,
    via: str,
    relpath: str,
    node: Node,
    line_starts: List[int],
    referrer: Optional[str],
) -> dict:
    return {
        "kind": kind,
        "ref": ref,
        "via": via,
        "file": relpath,
        "line": _line(node, line_starts),
        "referrer": referrer,
    }


def _is_texture_path(value: str) -> bool:
    """`picture = foo.dds` in leader-creation effects is a texture file path, not a sprite id."""
    return value.lower().endswith((".dds", ".tga", ".png"))


def child_get(node: Node, name: str) -> Optional[Node]:
    for c in node.children():
        if c.name == name:
            return c
    return None


def _symbol_or_str(node: Optional[Node]) -> Optional[str]:
    if node is None:
        return None
    v = node.value
    if isinstance(v, SymbolNode):
        return v.name
    if isinstance(v, str) and v:
        return v
    return None


def _line(node: Node, line_starts: List[int]) -> Optional[int]:
    tok = node.name_token
    if tok is None:
        return None
    return bisect.bisect_right(line_starts, tok.start)


def _line_starts(text: str) -> List[int]:
    starts = [0]
    for i, c in enumerate(text):
        if c == "\n":
            starts.append(i + 1)
    return starts
