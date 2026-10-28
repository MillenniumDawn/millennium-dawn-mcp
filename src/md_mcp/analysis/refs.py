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
from typing import Optional

from ..content_registry import (
    REFERENCE_EXTENSIONS,
    REFERENCE_KIND_ALIASES,
    REFERENCE_PATTERNS,
    REFERENCE_SCAN_DIRS,
)
from ..util.line_numbers import line_and_column, line_starts
from ..util.pathing import roots_for
from ..util.response import enforce_budget
from .text_cache import read_text

logger = logging.getLogger(__name__)

Kind = str


_PATTERNS = REFERENCE_PATTERNS
_SCAN_DIRS = REFERENCE_SCAN_DIRS
_EXTENSIONS = REFERENCE_EXTENSIONS
KIND_ALIASES = REFERENCE_KIND_ALIASES
_character_pattern = REFERENCE_PATTERNS["character"]
_country_tag_pattern = REFERENCE_PATTERNS["country_tag"]
_decision_pattern = REFERENCE_PATTERNS["decision"]
_event_pattern = REFERENCE_PATTERNS["event"]
_flag_pattern = REFERENCE_PATTERNS["flag"]
_focus_pattern = REFERENCE_PATTERNS["focus"]
_idea_pattern = REFERENCE_PATTERNS["idea"]
_loc_pattern = REFERENCE_PATTERNS["loc"]
_named_definition_pattern = REFERENCE_PATTERNS["scripted_effect"]
_sprite_pattern = REFERENCE_PATTERNS["sprite"]
_trait_pattern = REFERENCE_PATTERNS["trait"]
_variable_pattern = REFERENCE_PATTERNS["variable"]


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
# HOI4 reads a loc value from the first quote to the last quote on its line.
_LOC_TOKEN_RE = re.compile(r'(?P<comment>\#.*)|(?P<string>"[^\n]*")')


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
    tokens = (_LOC_TOKEN_RE if loc_yaml else _SCRIPT_TOKEN_RE).finditer(text)
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
