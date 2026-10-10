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
  sprite    — `icon` / `picture` (tries the raw name, then `GFX_<name>`, then
              `GFX_idea_<name>` — HOI4 resolves idea `picture` fields as
              `GFX_idea_<picture>`)
  loc       — `<focus_id>` and `<focus_id>_desc` for every focus defined in
              scope, plus `custom_effect_tooltip` keys
  decision  — `activate_decision`, `unlock_decision_tooltip`
  country_tag — tag fields such as `original_tag` and `change_tag`.
              `set_cosmetic_tag` takes a cosmetic-tag *name* (loc key), not a
              country tag, so it is intentionally not audited here.
  character — character fields such as `character` and `has_character`
  trait — leader-trait fields such as `add_trait` and `has_trait`

Only known direct scripted calls are counted; undefined calls are not detected.
`not_checked` lists scripted calls, country flags, and variables. If vanilla is
not configured, vanilla-defined ids may show as unresolved (`vanilla_indexed`).
The committed `vanilla_sprites` manifest resolves vanilla-only sprites without
an install (`vanilla_manifest`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..indexes import (
    CharacterIndex,
    CountryTagIndex,
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
    ScriptedEffectIndex,
    ScriptedTriggerIndex,
    TraitIndex,
)
from ..paradox.nodes import Node, node_line, symbol_or_str
from ..util.line_numbers import line_starts
from ..util.response import enforce_budget
from .refs import KIND_ALIASES
from .scope import iter_scope_files

_DEFAULT_KINDS: tuple = (
    "focus",
    "event",
    "idea",
    "sprite",
    "loc",
    "decision",
    "country_tag",
    "character",
    "trait",
    "scripted_effect",
    "scripted_trigger",
)
# Reused icons are common (163 groups in 05_usa.txt), so this one is opt-in.
_ALL_KINDS: tuple = (*_DEFAULT_KINDS, "duplicate_icons")
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
_COUNTRY_TAG_NODES = frozenset(
    {
        "original_tag",
        "tag",
        "change_tag",
        "target_tag",
        "original_tag_to_check",
        "tag_to_check",
    }
)
_SCOPE_KEYWORDS = frozenset({"ROOT", "FROM", "PREV", "THIS", "OWNER", "CONTROLLER"})
_DOTTED_SCOPE_PREFIXES = ("var:", "event_target:")
_CHARACTER_NODES = frozenset(
    {
        "character",
        "has_character",
        "create_character",
        "remove_character",
        "modify_character",
        "set_character",
    }
)
_TRAIT_NODES = frozenset({"trait", "has_trait", "add_trait", "remove_trait", "remove_leader_trait"})
_FOCUS_DEF_NODES = frozenset({"focus", "shared_focus", "joint_focus"})

# Kinds whose refs are a plain symbol under one of the listed node names. The
# name sets are mutually disjoint, so at most one entry matches a given child.
_SYMBOL_REF_NODES = (
    ("focus", _FOCUS_SYMBOL_NODES),
    ("idea", _IDEA_SYMBOL_NODES),
    ("loc", _LOC_NODES),
    ("decision", _DECISION_NODES),
)


def check_refs(
    mod_root: Path,
    *,
    focus_index: FocusIndex,
    event_index: EventIndex,
    idea_index: IdeaIndex,
    gfx_index: GfxIndex,
    loc_index: LocalisationIndex,
    decision_index: DecisionIndex,
    country_tag_index: Optional[CountryTagIndex] = None,
    character_index: Optional[CharacterIndex] = None,
    trait_index: Optional[TraitIndex] = None,
    scripted_effect_index: Optional[ScriptedEffectIndex] = None,
    scripted_trigger_index: Optional[ScriptedTriggerIndex] = None,
    tag: Optional[str] = None,
    files: Optional[list[str]] = None,
    kinds: Optional[Sequence[str]] = None,
    vanilla_path: Optional[Path] = None,
    submod_root: Optional[Path] = None,
    vanilla_sprites: Optional[frozenset[str]] = None,
    lang: str = "en",
    limit: int = 200,
    offset: int = 0,
    counts_only: bool = False,
) -> dict:
    """Audit cross-references in the given scope.

    Scope: `files=[...]` (mod-relative paths, any script type) or `tag=` (the
    tag's prefix-matched focus files; use `files=` to audit event/decision
    files). Unresolved refs are deduped by (kind, id) with an occurrence count
    and first sites. `limit=-1` returns the full unresolved list, guarded only
    by `enforce_budget`.
    """
    if not tag and not files:
        return {"ok": False, "error": "Pass tag= or files=[...] (mod-relative paths)."}

    selected = list(kinds) if kinds else list(_DEFAULT_KINDS)
    selected = [KIND_ALIASES.get(kind, kind) for kind in selected]
    unknown = [k for k in selected if k not in _ALL_KINDS]
    if unknown:
        return {"ok": False, "error": f"Unknown kind(s): {unknown}. Valid: {list(_ALL_KINDS)}"}
    selected_set = set(selected)

    if files:
        scope_files = list(files)
    else:
        # tag is guaranteed set here: `not tag and not files` returned above.
        # pi-lens-ignore: python-assert-production
        assert tag is not None
        scope_files = focus_index.files_for_tag(tag)

    files_truncated = len(scope_files) > _MAX_FILES
    scope_files = scope_files[:_MAX_FILES]

    indexes: dict[str, Any] = {
        "focus": focus_index,
        "event": event_index,
        "idea": idea_index,
        "sprite": gfx_index,
        "loc": loc_index,
        "decision": decision_index,
        "country_tag": country_tag_index,
        "character": character_index,
        "trait": trait_index,
        "scripted_effect": scripted_effect_index,
        "scripted_trigger": scripted_trigger_index,
    }
    for kind in selected_set:
        index = indexes.get(kind)
        if index is not None:
            index.ensure_fresh()

    scripted_effect_names = (
        set(scripted_effect_index.list_keys()) if scripted_effect_index else set()
    )
    scripted_trigger_names = (
        set(scripted_trigger_index.list_keys()) if scripted_trigger_index else set()
    )

    # Collect raw references: (kind, ref, via, file, line, referrer).
    refs: list[dict] = []
    parse_errors: list[dict] = []
    focus_defs: list[dict] = []  # focus ids defined in scope, for loc coverage
    focus_icons: list[dict] = []

    for parsed in iter_scope_files(
        scope_files, mod_root, vanilla_path, parse_errors, submod_root=submod_root
    ):
        starts = line_starts(parsed.text)
        _walk(
            parsed.root,
            parsed.relpath,
            starts,
            selected_set,
            refs,
            focus_defs,
            focus_icons,
            referrer=None,
            scripted_effect_names=scripted_effect_names,
            scripted_trigger_names=scripted_trigger_names,
        )

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

    # Group per file, as upstream `tools/assets/duplicate_icon.py` does.
    icon_groups: dict[tuple[str, str], dict] = {}
    for entry in focus_icons:
        group = icon_groups.setdefault(
            (entry["file"], entry["icon"].casefold()), {"icon": entry["icon"], "focuses": []}
        )
        group["focuses"].append({k: entry[k] for k in ("id", "file", "line")})
    duplicate_icons = [
        icon_groups[key] for key in sorted(icon_groups) if len(icon_groups[key]["focuses"]) > 1
    ]

    vanilla_sprites_set = vanilla_sprites or frozenset()
    resolvers: dict[str, Callable[[str], bool]] = {
        "focus": lambda r: focus_index.resolve(r) is not None,
        "event": lambda r: event_index.resolve(r) is not None,
        "idea": lambda r: idea_index.resolve(r) is not None,
        "sprite": lambda r: (
            gfx_index.resolve(r) is not None
            or gfx_index.resolve(f"GFX_{r}") is not None
            or gfx_index.resolve(f"GFX_idea_{r}") is not None
            or r in vanilla_sprites_set
            or f"GFX_{r}" in vanilla_sprites_set
            or f"GFX_idea_{r}" in vanilla_sprites_set
        ),
        "loc": lambda r: loc_index.resolve(r, lang) is not None,
        "decision": lambda r: decision_index.resolve(r) is not None,
        "country_tag": lambda r: (
            country_tag_index is not None and country_tag_index.resolve(r) is not None
        ),
        "character": lambda r: (
            character_index is not None and character_index.resolve(r) is not None
        ),
        "trait": lambda r: trait_index is not None and trait_index.resolve(r) is not None,
        "scripted_effect": lambda r: (
            scripted_effect_index is not None and scripted_effect_index.resolve(r) is not None
        ),
        "scripted_trigger": lambda r: (
            scripted_trigger_index is not None and scripted_trigger_index.resolve(r) is not None
        ),
    }

    checked: dict[str, set[str]] = {k: set() for k in selected}
    unresolved_by_key: dict[tuple, dict] = {}
    resolved_cache: dict[tuple, bool] = {}

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
        entry = unresolved_by_key.setdefault(
            key, {"kind": kind, "ref": ref, "count": 0, "sites": []}
        )
        entry["count"] += 1
        if len(entry["sites"]) < 3:
            site = {"file": r["file"], "line": r["line"], "via": r["via"]}
            if r.get("referrer"):
                site["referrer"] = r["referrer"]
            entry["sites"].append(site)

    unresolved = sorted(unresolved_by_key.values(), key=lambda e: (e["kind"], e["ref"]))
    total = len(unresolved)
    offset = max(offset, 0)
    end = offset + limit if limit >= 0 else None
    sliced = unresolved[offset:end]

    result: dict = {
        "ok": True,
        "scope": {"tag": tag.upper()} if tag and not files else {"files": len(scope_files)},
        "files_scanned": len(scope_files),
        "files_truncated": files_truncated,
        "kinds_checked": selected,
        "not_checked": [
            "country_flags",
            "variables",
            "scripted_effects",
            "scripted_triggers",
        ],
        "vanilla_indexed": vanilla_path is not None,
        "vanilla_manifest": vanilla_sprites is not None,
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
    if "duplicate_icons" in selected_set:
        page = duplicate_icons[offset:end]
        result["counts"]["duplicate_icons"] = {
            "checked": len(focus_icons),
            "unresolved": len(duplicate_icons),
        }
        result["total_duplicate_icons"] = len(duplicate_icons)
        result["returned_duplicate_icons"] = len(page)
        result["duplicate_icons_truncated"] = offset + len(page) < len(duplicate_icons)
        if not counts_only:
            result["duplicate_icons"] = page

    return enforce_budget(result, heavy_keys=("duplicate_icons", "unresolved", "parse_errors"))


def _walk(
    node: Node,
    relpath: str,
    starts: list[int],
    kinds: set[str],
    refs: list[dict],
    focus_defs: list[dict],
    focus_icons: list[dict],
    referrer: Optional[str],
    *,
    scripted_effect_names: set[str],
    scripted_trigger_names: set[str],
) -> None:
    for child in node.children():
        name = child.name
        ctx = referrer

        if name in _FOCUS_DEF_NODES:
            fid = _symbol_or_str(_child_get(child, "id"))
            if fid:
                ctx = fid
                focus_defs.append({"id": fid, "file": relpath, "line": node_line(child, starts)})
                icon_node = _child_get(child, "icon") if "duplicate_icons" in kinds else None
                icon = _symbol_or_str(icon_node)
                if icon and icon_node is not None:
                    focus_icons.append(
                        {
                            "id": fid,
                            "icon": icon,
                            "file": relpath,
                            "line": node_line(icon_node, starts),
                        }
                    )

        if "focus" in kinds and name in ("prerequisite", "mutually_exclusive"):
            for m in child.children():
                if m.name == "focus":
                    ref = _symbol_or_str(m)
                    if ref:
                        refs.append(_ref("focus", ref, name, relpath, m, starts, ctx))

        if "event" in kinds and name in _EVENT_NODES:
            ref = _symbol_or_str(child)
            if ref is None and isinstance(child.value, list):
                ref = _symbol_or_str(_child_get(child, "id"))
            if ref:
                refs.append(_ref("event", ref, name, relpath, child, starts, ctx))

        if "idea" in kinds and name in _IDEA_BLOCK_NODES:
            ref = _symbol_or_str(child)
            if ref:
                refs.append(_ref("idea", ref, name, relpath, child, starts, ctx))
            elif isinstance(child.value, list):
                for m in child.children():
                    # bare symbols inside the block parse as name-only nodes
                    if m.value is None and m.name:
                        refs.append(_ref("idea", m.name, name, relpath, m, starts, ctx))

        if "sprite" in kinds and name in _SPRITE_NODES:
            ref = _symbol_or_str(child)
            if ref is None and isinstance(child.value, list):
                for m in child.children():
                    if m.name == "value":
                        v = _symbol_or_str(m)
                        if v and not _is_texture_path(v):
                            refs.append(_ref("sprite", v, name, relpath, m, starts, ctx))
            elif ref and not _is_texture_path(ref):
                refs.append(_ref("sprite", ref, name, relpath, child, starts, ctx))

        for kind, names in _SYMBOL_REF_NODES:
            if kind in kinds and name in names:
                ref = _symbol_or_str(child)
                if ref:
                    refs.append(_ref(kind, ref, name, relpath, child, starts, ctx))

        for kind, names in (
            ("country_tag", _COUNTRY_TAG_NODES),
            ("character", _CHARACTER_NODES),
            ("trait", _TRAIT_NODES),
        ):
            if kind in kinds and name in names:
                ref = _symbol_or_str(child)
                if ref is None and isinstance(child.value, list):
                    if kind == "country_tag":
                        ref = _symbol_or_str(_child_get(child, "tag")) or _symbol_or_str(
                            _child_get(child, "original_tag")
                        )
                    else:
                        ref = _symbol_or_str(_child_get(child, kind))
                if ref and (kind != "country_tag" or not _is_scope_reference(ref)):
                    refs.append(_ref(kind, ref, name, relpath, child, starts, ctx))

        for kind, defined in (
            ("scripted_effect", scripted_effect_names),
            ("scripted_trigger", scripted_trigger_names),
        ):
            if kind not in kinds:
                continue
            if name not in defined:
                continue
            # The key names the scripted definition; the value holds its arguments.
            refs.append(_ref(kind, name, name, relpath, child, starts, ctx))

        if isinstance(child.value, list):
            _walk(
                child,
                relpath,
                starts,
                kinds,
                refs,
                focus_defs,
                focus_icons,
                ctx,
                scripted_effect_names=scripted_effect_names,
                scripted_trigger_names=scripted_trigger_names,
            )


def _ref(
    kind: str,
    ref: str,
    via: str,
    relpath: str,
    node: Node,
    starts: list[int],
    referrer: Optional[str],
) -> dict:
    return {
        "kind": kind,
        "ref": ref,
        "via": via,
        "file": relpath,
        "line": node_line(node, starts),
        "referrer": referrer,
    }


def _is_texture_path(value: str) -> bool:
    """`picture = foo.dds` in leader-creation effects is a texture file path, not a sprite id."""
    return value.lower().endswith((".dds", ".tga", ".png"))


def _is_scope_reference(value: str) -> bool:
    """Country scope names, scope chains, and dynamic accessors are not tag ids."""
    return (
        value in _SCOPE_KEYWORDS
        or value.startswith(_DOTTED_SCOPE_PREFIXES)
        or ("." in value and all(part.upper() in _SCOPE_KEYWORDS for part in value.split(".")))
    )


def _child_get(node: Node, name: str) -> Optional[Node]:
    for c in node.children():
        if c.name == name:
            return c
    return None


def _symbol_or_str(node: Optional[Node]) -> Optional[str]:
    return symbol_or_str(node) or None
