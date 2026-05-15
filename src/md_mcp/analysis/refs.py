"""Find-all-references: reverse lookup of every reference to an ID.

Implemented as a regex grep over the relevant subdirectories — paths to scan are
chosen based on the reference `kind`. Patterns are anchored to word boundaries and,
where possible, to the keyword that introduces the reference (e.g. `has_idea = X`).

This is the kind of question the agent currently has to answer with shell `grep` —
calling a dedicated tool returns structured matches and prevents accidentally matching
strings inside comments or unrelated identifiers.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, List, Literal, Optional

logger = logging.getLogger(__name__)

Kind = Literal["focus", "event", "decision", "idea", "loc", "sprite", "flag", "variable"]


# For each kind, define:
#   * subdirs to scan (mod-relative)
#   * extensions to match (.txt usually; .yml for loc)
#   * a regex generator that, given the target id, returns the pattern to search for
def _focus_pattern(target: str) -> re.Pattern:
    # `focus = X`, `prerequisite = { focus = X }`, `mutually_exclusive = { focus = X }`,
    # `complete_national_focus = X`, `mark_focus_tree_layout_dirty` style false-positives skipped.
    return re.compile(
        r"\b(?:focus|complete_national_focus|unlock_national_focus)\s*=\s*"
        + re.escape(target)
        + r"\b"
    )


def _event_pattern(target: str) -> re.Pattern:
    # `country_event = X`, `country_event = { id = X ... }`, `news_event = X`, etc.
    return re.compile(
        r"\b(?:country_event|news_event|state_event|unit_leader_event|operative_leader_event)\s*=\s*(?:"
        + re.escape(target)
        + r"\b"
        + r"|\{\s*(?:[^{}]*\s)?id\s*=\s*"
        + re.escape(target)
        + r"\b)"
    )


def _decision_pattern(target: str) -> re.Pattern:
    # `activate_decision = X`, `unlock_decision_tooltip = X`, decision target in tooltips.
    return re.compile(
        r"\b(?:activate_decision|unlock_decision_tooltip|remove_decision|decision)\s*=\s*"
        + re.escape(target)
        + r"\b"
    )


def _idea_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:has_idea|add_ideas|remove_ideas|swap_ideas|add_timed_idea)\s*=\s*"
        + r"(?:"
        + re.escape(target)
        + r"\b"
        + r"|\{\s*(?:[^{}]*\s)?(?:idea|add)\s*=\s*"
        + re.escape(target)
        + r"\b)"
    )


def _flag_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:has_country_flag|set_country_flag|clr_country_flag|modify_country_flag"
        r"|has_global_flag|set_global_flag|clr_global_flag|modify_global_flag"
        r"|has_state_flag|set_state_flag|clr_state_flag"
        r"|has_character_flag|set_character_flag|clr_character_flag)\s*=\s*"
        + r"(?:"
        + re.escape(target)
        + r"\b"
        + r"|\{\s*flag\s*=\s*"
        + re.escape(target)
        + r"\b)"
    )


def _variable_pattern(target: str) -> re.Pattern:
    # Bare references (`check_variable = { var = X ... }`) plus dotted refs (`ROOT.X`).
    return re.compile(
        r"\b(?:var|set_variable|set_temp_variable|add_to_variable|subtract_from_variable"
        r"|multiply_variable|divide_variable|check_variable|clamp_variable|modulo_variable)\s*=\s*"
        + r"(?:"
        + re.escape(target)
        + r"\b"
        + r"|\{\s*(?:[^{}]*\s)?(?:var|name)\s*=\s*"
        + re.escape(target)
        + r"\b)"
    )


def _loc_pattern(target: str) -> re.Pattern:
    # Both `KEY:` definitions and `[KEY]` / `$KEY$` references.
    return re.compile(
        r"(?<![\w])" + re.escape(target) + r"(?![\w])"
    )


def _sprite_pattern(target: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(target) + r"\b")


_PATTERNS = {
    "focus": _focus_pattern,
    "event": _event_pattern,
    "decision": _decision_pattern,
    "idea": _idea_pattern,
    "loc": _loc_pattern,
    "sprite": _sprite_pattern,
    "flag": _flag_pattern,
    "variable": _variable_pattern,
}


# Directories to scan per reference kind. Keep the set small to minimise false positives.
_SCAN_DIRS = {
    "focus": [
        "common/national_focus",
        "events",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/on_actions",
        "common/ideas",
    ],
    "event": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/on_actions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/ideas",
    ],
    "decision": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/on_actions",
        "common/scripted_effects",
        "common/scripted_triggers",
    ],
    "idea": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/ideas",
        "history/countries",
    ],
    "flag": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/ideas",
    ],
    "variable": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/ideas",
    ],
    # Loc keys are *defined* in localisation/, but *referenced* across all script
    # files (focus titles, event descs, tooltips, custom_effect_tooltip targets).
    "loc": [
        "localisation/english",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/ideas",
        "common/scripted_localisation",
        "common/scripted_effects",
        "common/scripted_triggers",
    ],
    "sprite": [
        "common/national_focus",
        "common/decisions",
        "common/ideas",
        "interface",
    ],
}

_EXTENSIONS = {
    # Loc keys appear in both the defining .yml files and as bare identifiers in
    # script (.txt) files used as titles / descriptions / tooltip targets.
    "loc": (".yml", ".txt"),
    "sprite": (".txt", ".gui", ".gfx"),
}


def find_references(
    mod_root: Path,
    kind: Kind,
    target: str,
    *,
    vanilla_path: Optional[Path] = None,
    include_vanilla: bool = False,
    limit: int = 100,
    offset: int = 0,
    snippet_chars: int = 120,
    files_only: bool = False,
) -> dict:
    """Return every match of `target` in the files relevant to its reference kind.

    `files_only=True` collapses results to a unique file list with hit counts,
    which is far smaller than a full match list for hot loc keys or sprites.
    `offset` + `limit` paginate the match list. The scan still runs to
    completion so `total` is accurate; only the returned slice is bounded.
    """
    from ..util.response import enforce_budget  # local import; avoids cycle

    if kind not in _PATTERNS:
        return {
            "ok": False,
            "error": f"Unknown kind '{kind}'. Use one of: {sorted(_PATTERNS)}",
        }

    pattern = _PATTERNS[kind](target)
    exts = _EXTENSIONS.get(kind, (".txt",))
    scan_dirs = _SCAN_DIRS[kind]

    roots = [mod_root] + ([vanilla_path] if include_vanilla and vanilla_path else [])

    matches: List[dict] = []
    file_hits: dict[str, int] = {}
    # Hard scan cap — protects against pathological short loc-key matches that
    # have hundreds of thousands of hits across the whole mod.
    scan_cap = max(limit + offset, 100) * 50 if not files_only else 100_000

    scan_truncated = False
    for base in roots:
        if scan_truncated:
            break
        for sub in scan_dirs:
            if scan_truncated:
                break
            d = base / sub
            if not d.is_dir():
                continue
            for ext in exts:
                if scan_truncated:
                    break
                for path in d.rglob(f"*{ext}"):
                    if scan_truncated:
                        break
                    if not path.is_file():
                        continue
                    try:
                        text = path.read_bytes().decode("utf-8", errors="replace")
                    except OSError:
                        continue

                    if text.startswith("﻿"):
                        text = text[1:]

                    try:
                        rel = str(path.relative_to(base))
                    except ValueError:
                        rel = str(path)

                    for m in pattern.finditer(text):
                        if files_only:
                            file_hits[rel] = file_hits.get(rel, 0) + 1
                            continue
                        line, col = _pos_to_lc(text, m.start())
                        snippet = _line_at(text, m.start()).strip()[:snippet_chars]
                        matches.append(
                            {
                                "file": rel,
                                "line": line,
                                "col": col,
                                "snippet": snippet,
                            }
                        )
                        if len(matches) >= scan_cap:
                            scan_truncated = True
                            break

    if files_only:
        files_list = sorted(
            ({"file": f, "hits": n} for f, n in file_hits.items()),
            key=lambda r: (-r["hits"], r["file"]),
        )
        total = len(files_list)
        sliced = files_list[offset : offset + limit]
        return enforce_budget(
            {
                "ok": True,
                "kind": kind,
                "target": target,
                "mode": "files_only",
                "total_files": total,
                "files_returned": len(sliced),
                "truncated": (offset + limit) < total,
                "scan_truncated": scan_truncated,
                "files": sliced,
            },
            heavy_keys=("files",),
        )

    total = len(matches)
    sliced = matches[offset : offset + limit]
    return enforce_budget(
        {
            "ok": True,
            "kind": kind,
            "target": target,
            "total": total,
            "returned": len(sliced),
            "truncated": (offset + limit) < total,
            "scan_truncated": scan_truncated,
            "matches": sliced,
        },
        heavy_keys=("matches",),
    )


def _pos_to_lc(text: str, pos: int) -> tuple[int, int]:
    line = text.count("\n", 0, pos) + 1
    line_start = text.rfind("\n", 0, pos) + 1
    return line, pos - line_start + 1


def _line_at(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end]
