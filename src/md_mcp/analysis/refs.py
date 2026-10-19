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
from typing import Literal, Optional

from ..paradox.schema import EVENT_KINDS
from ..util.line_numbers import line_and_column, line_starts
from ..util.pathing import roots_for
from ..util.response import enforce_budget
from .text_cache import read_text

logger = logging.getLogger(__name__)

Kind = Literal[
    "focus",
    "event",
    "decision",
    "idea",
    "loc",
    "sprite",
    "flag",
    "variable",
    "country_tag",
    "tag",
    "character",
    "trait",
    "scripted_effect",
    "scripted_trigger",
    "scripted_effects",
    "scripted_triggers",
]


# For each kind, define:
#   * subdirs to scan (mod-relative)
#   * extensions to match (.txt usually; .yml for loc)
#   * a regex generator that, given the target id, returns the pattern to search for
def _identifier(target: str) -> str:
    return _target_group(re.escape(target))


def _target_group(body: str) -> str:
    # The only capture in each pattern: `_active_matches` judges this span.
    return r'(?<![\w.])("' + body + r'"|' + body + r")(?![\w.])"


def _focus_pattern(target: str) -> re.Pattern:
    # `focus = X`, `prerequisite = { focus = X }`, `mutually_exclusive = { focus = X }`,
    # `complete_national_focus = X`, `mark_focus_tree_layout_dirty` style false-positives skipped.
    return re.compile(
        r"\b(?:focus|complete_national_focus|unlock_national_focus)\s*=\s*" + _identifier(target)
    )


def _event_pattern(target: str) -> re.Pattern:
    # `country_event = X`, `country_event = { id = X ... }`, `news_event = X`, etc.
    event_kinds = "|".join(re.escape(kind) for kind in EVENT_KINDS)
    return re.compile(
        rf"\b(?:{event_kinds})\s*=\s*(?:"
        + _identifier(target)
        + r"|\{\s*(?:[^{}]*\s)?id\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _decision_pattern(target: str) -> re.Pattern:
    # `activate_decision = X`, `unlock_decision_tooltip = X`, decision target in tooltips.
    return re.compile(
        r"\b(?:activate_decision|unlock_decision_tooltip|remove_decision|decision)\s*=\s*"
        + _identifier(target)
    )


def _idea_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:has_idea|add_ideas|remove_ideas|swap_ideas|add_timed_idea)\s*=\s*"
        + r"(?:"
        + _identifier(target)
        + r"|\{\s*(?:[^{}]*\s)?(?:idea|add)\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _flag_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:has_country_flag|set_country_flag|clr_country_flag|modify_country_flag"
        r"|has_global_flag|set_global_flag|clr_global_flag|modify_global_flag"
        r"|has_state_flag|set_state_flag|clr_state_flag"
        r"|has_character_flag|set_character_flag|clr_character_flag)\s*=\s*"
        + r"(?:"
        + _identifier(target)
        + r"|\{\s*flag\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _variable_pattern(target: str) -> re.Pattern:
    # References can be explicit (`var = X`), scoped (`var = ROOT.X`),
    # or short-form assignments (`set_temp_variable = { X = 10 }`).
    base = target.rsplit(".", 1)[-1]
    target_ref = _identifier(target)
    if "." not in target:
        target_ref = _target_group(r"(?:[A-Za-z_]\w*\.)?" + re.escape(base))
    operators = (
        r"var|has_variable|set_variable|set_temp_variable|add_to_variable|subtract_from_variable"
        r"|multiply_variable|divide_variable|check_variable|clamp_variable|modulo_variable"
    )
    return re.compile(
        r"\b(?:"
        + operators
        + r")\s*=\s*(?:"
        + target_ref
        + r"|\{\s*(?:[^{}]*?\s)?(?:var|name)\s*=\s*"
        + target_ref
        + r"(?:\s|\})|\{\s*(?:[^{}]*?\s)?"
        + target_ref
        + r"\s*=)"
    )


def _loc_pattern(target: str) -> re.Pattern:
    # Both `KEY:` definitions and `[KEY]` / `$KEY$` references.
    return re.compile(_identifier(target))


def _sprite_pattern(target: str) -> re.Pattern:
    return re.compile(_identifier(target))


def _named_definition_pattern(target: str) -> re.Pattern:
    """Match a top-level definition or direct scripted call by name."""
    return re.compile(_identifier(target) + r"\s*=\s*(?:\{|[^\s{}#]+)")


def _country_tag_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:original_tag|tag|change_tag|set_cosmetic_tag|target_tag|"
        r"original_tag_to_check|tag_to_check)\s*=\s*(?:"
        + _identifier(target)
        + r"|\{\s*(?:tag|original_tag)\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _character_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:character|has_character|create_character|remove_character|"
        r"modify_character|set_character)\s*=\s*(?:"
        + _identifier(target)
        + r"|\{\s*character\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _trait_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:trait|has_trait|add_trait|remove_trait|remove_leader_trait)\s*=\s*(?:"
        + _identifier(target)
        + r"|\{\s*trait\s*=\s*"
        + _identifier(target)
        + r")"
    )


_PATTERNS = {
    "focus": _focus_pattern,
    "event": _event_pattern,
    "decision": _decision_pattern,
    "idea": _idea_pattern,
    "loc": _loc_pattern,
    "sprite": _sprite_pattern,
    "flag": _flag_pattern,
    "variable": _variable_pattern,
    "country_tag": _country_tag_pattern,
    "character": _character_pattern,
    "trait": _trait_pattern,
    "scripted_effect": _named_definition_pattern,
    "scripted_trigger": _named_definition_pattern,
}

KIND_ALIASES = {
    "tag": "country_tag",
    "scripted_effects": "scripted_effect",
    "scripted_triggers": "scripted_trigger",
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
    "country_tag": [
        "common/country_tags",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/ideas",
        "common/scripted_effects",
        "common/scripted_triggers",
        "history/countries",
    ],
    "character": [
        "common/characters",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "history/countries",
        "history/units",
    ],
    "trait": [
        "common/country_leader",
        "common/unit_leader",
        "common/characters",
        "common/national_focus",
        "events",
        "common/scripted_effects",
        "common/scripted_triggers",
        "history/units",
    ],
    "scripted_effect": [
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/ideas",
        "common/on_actions",
        "history/countries",
    ],
    "scripted_trigger": [
        "common/scripted_triggers",
        "common/scripted_effects",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/ideas",
        "common/on_actions",
        "history/countries",
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
    submod_root: Optional[Path] = None,
    limit: int = 100,
    offset: int = 0,
    snippet_chars: int = 120,
    files_only: bool = False,
    prefilter: bool = True,
) -> dict:
    """Return every match of `target` in the files relevant to its reference kind.

    `files_only=True` collapses results to a unique file list with hit counts,
    which is far smaller than a full match list for hot loc keys or sprites.
    `offset` + `limit` paginate the match list. The scan still runs to
    completion so `total` is accurate; only the returned slice is bounded.
    A file whose relative path exists in a higher-priority root (submod, then
    mod, then vanilla) is hidden and not scanned.

    File contents come from a stat-keyed in-process cache (`text_cache`), and
    files that do not contain `target` as a literal substring skip the regex.
    That is exact, not a heuristic: every pattern in `_PATTERNS` embeds
    `re.escape(target)` as a mandatory literal (no IGNORECASE, and no optional
    or alternation branch that omits it), so a file lacking the raw target
    cannot match. `prefilter=False` disables the shortcut (equivalence tests).
    """
    canonical = KIND_ALIASES.get(kind, kind)
    if canonical not in _PATTERNS:
        return {
            "ok": False,
            "error": f"Unknown kind '{kind}'. Use one of: {sorted([*_PATTERNS, *KIND_ALIASES])}",
        }

    pattern = _PATTERNS[canonical](target)
    exts = _EXTENSIONS.get(canonical, (".txt",))
    scan_dirs = _SCAN_DIRS[canonical]

    roots = roots_for(mod_root, vanilla_path if include_vanilla else None, submod_root)

    matches: list[dict] = []
    file_hits: dict[str, int] = {}
    # Hard scan cap — protects against pathological short loc-key matches that
    # have hundreds of thousands of hits across the whole mod.
    scan_cap = max(limit + offset, 100) * 50 if not files_only else 100_000

    scan_truncated = False
    seen: set[str] = set()  # relative paths owned by a higher-priority root
    for base in roots:
        if scan_truncated:
            break
        shadowed = frozenset(seen)
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
                    rel = str(path.relative_to(base))
                    seen.add(rel)
                    if rel in shadowed:
                        continue
                    text = read_text(path)  # BOM already stripped at cache-fill time
                    if text is None:
                        continue
                    if prefilter and target not in text:
                        continue

                    loc_yaml = canonical == "loc" and ext == ".yml"
                    starts: Optional[list[int]] = None
                    for m in _active_matches(
                        pattern,
                        text,
                        target,
                        loc_yaml=loc_yaml,
                        allow_quoted_scopes=canonical == "variable",
                    ):
                        if files_only:
                            file_hits[rel] = file_hits.get(rel, 0) + 1
                            continue
                        if starts is None:
                            starts = line_starts(text)
                        line, col = line_and_column(m.start(), starts)
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
        files_list = [
            {"file": f, "hits": n}
            for f, n in sorted(file_hits.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
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


_SCRIPT_TOKEN_RE = re.compile(r'(?P<comment>\#.*)|(?P<string>"(?:\\.|[^"\\])*")')


def _active_matches(
    pattern: re.Pattern,
    text: str,
    target: str,
    *,
    loc_yaml: bool = False,
    allow_quoted_scopes: bool = False,
):
    """Yield matches whose keyword is code and whose target is code or a quoted value.

    Comments and strings between the keyword and the target do not count.
    """
    tokens = _SCRIPT_TOKEN_RE.finditer(text)
    token = next(tokens, None)

    def token_at(pos: int) -> Optional[re.Match]:
        nonlocal token
        while token is not None and token.end() <= pos:
            token = next(tokens, None)
        return token if token is not None and token.start() <= pos else None

    for match in pattern.finditer(text):
        start, end = match.span(match.lastindex or 0)
        if start > match.start() and token_at(match.start()) is not None:
            continue
        found = token_at(start)
        if found is None:
            yield match
            continue
        if found.lastgroup == "comment":
            continue
        content = text[found.start() + 1 : found.end() - 1]
        quoted_scope = (
            allow_quoted_scopes
            and "." not in target
            and re.fullmatch(r"[A-Za-z_]\w*\." + re.escape(target), content) is not None
        )
        loc_marker = loc_yaml and (text[start - 1 : start], text[end : end + 1]) in (
            ("$", "$"),
            ("[", "]"),
        )
        if content == target or quoted_scope or loc_marker:
            yield match


def _line_at(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end]
