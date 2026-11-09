"""Reverse query: names defined in one file that nothing references.

`idea` and `scripted_loc` port the matching of upstream `find_idea_references.py` and
`find_scripted_loc_references.py` so counts agree with those scripts, quirks included
(a commented reference counts). `scripted_effect` and `scripted_trigger` have no
upstream finder: they use the `find_references` semantics plus the tooltip, meta template,
and allowlist rules of `validate_unused_scripted`, in one pass over all names.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
from bisect import bisect_right
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..content_registry import REFERENCE_PATTERNS
from ..indexes.base import GenericTxtIndex
from ..util.line_numbers import line_starts, pos_to_line
from ..util.pathing import roots_for, validate_user_path
from ..util.response import coerce_int, enforce_budget, paginate
from .refs import _SCRIPT_TOKEN_RE
from .text_cache import read_text

KINDS = ("idea", "scripted_loc", "scripted_effect", "scripted_trigger")
_INDEXED = ("scripted_effect", "scripted_trigger")
MAX_SAMPLES = 3
SNIPPET_CHARS = 120

_WORD = re.compile(r"\w+")
_BRACKETED = re.compile(r"\[(\w+)\]")

# find_idea_references.py
_IDEA_DIRS = (
    "common/events",
    "common/decisions",
    "common/national_focus",
    "history/countries",
    "common/scripted_effects",
    "events",
    "common/on_actions",
    "common/focuses",
    "common/country_leader",
    "common/ideas",
)
_IDEA_PATTERNS = re.compile(
    "|".join(
        [
            "add_ideas",
            "remove_ideas",
            "has_idea",
            "swap_ideas",
            "modify_ideas",
            "add_timed_idea",
            "remove_timed_idea",
            r"idea.*=",
            r"=\s*idea",
        ]
    )
)
_IDEA_BLOCK = re.compile(
    r"\b(?:add_ideas|remove_ideas|swap_ideas|modify_ideas|add_timed_idea|remove_timed_idea)\b"
)
_IDEA_SKIP = frozenset(
    {
        "ideas",
        "country",
        "hidden_ideas",
        "modifier",
        "allowed",
        "allowed_civil_war",
        "cancel",
        "on_add",
        "on_remove",
        "equipment_bonus",
        "if",
        "limit",
        "OR",
        "AND",
        "NOT",
        "picture",
        "name",
    }
)
_IDEA_DEFINITION = re.compile(r"^\s+([A-Za-z][A-Za-z0-9_]+)\s*=\s*\{\s*$")
_TAG = re.compile(r"^[A-Z]{2,4}$")

# find_scripted_loc_references.py; the localisation directory goes last so its
# bulk does not fill the shared text cache before the script directories do.
_LOC_DIR = "localisation"
_LOC_DIRS = (
    "interface",
    "events",
    "common/decisions",
    "common/national_focus",
    "common/scripted_effects",
    "common/on_actions",
    "common/focuses",
    "common/scripted_guis",
    "gfx/interface",
    _LOC_DIR,
)
_LOC_DEFINITION = re.compile(r"^\s*name\s*=\s*([A-Za-z][A-Za-z0-9_]*)\s*$")

# validate_unused_scripted.py scans these, `.txt` only.
_CALL_DIRS = ("common", "events", "history")
# `_named_definition_pattern` with the name left open and the tail as a lookahead.
_CALL = re.compile(r'(?<![\w.])("\w+"|\w+)(?![\w.])(?=\s*=\s*(?:\{|[^\s{}#]))')
_CUSTOM_TOOLTIP = re.compile(r"custom_(?:effect|trigger)_tooltip\s*=\s*(\w+)")
# validator_common.py: `set_leader_[TAG] = yes` and `"[?x]_suffix"` in meta blocks.
_META_TEMPLATE = re.compile(
    r"(?<![/\"])\b([A-Za-z_][A-Za-z0-9_.]*(?:\[[A-Za-z_][A-Za-z0-9_]*\][A-Za-z0-9_.]*)+)"
)
_QUOTED_META_TEMPLATE = re.compile(r'"([^"]*\[[^\]]+\][^"]*)"')
_META_VARIABLE = re.compile(r"\[[^\]]+\]")

_SPECS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "idea": tuple((d, (".txt",)) for d in _IDEA_DIRS),
    "scripted_loc": tuple(
        (d, (".yml",) if d == _LOC_DIR else (".txt", ".gui", ".gfx", ".yml")) for d in _LOC_DIRS
    ),
    "scripted_effect": tuple((d, (".txt",)) for d in _CALL_DIRS),
    "scripted_trigger": tuple((d, (".txt",)) for d in _CALL_DIRS),
}

Hit = tuple[str, int, str]


@dataclass
class Analysis:
    file: str
    names: list[str]
    counts: dict[str, int]
    samples: dict[str, list[dict]]
    allowlisted: frozenset[str] = frozenset()


def find_unreferenced(
    mod_root: Path,
    kind: str,
    file: str,
    *,
    submod_root: Path | None = None,
    indexes: Mapping[str, GenericTxtIndex],
    limit: int | float | str | None = 200,
    offset: int | float | str | None = 0,
    include_hits: bool = False,
) -> dict:
    """Names defined in `file` that no other scanned file references, paged."""
    try:
        limit = max(coerce_int(limit, name="limit", default=200), 0)
        offset = max(coerce_int(offset, name="offset", default=0), 0)
        analysis = analyse(
            mod_root, kind, file, submod_root=submod_root, indexes=indexes, samples=include_hits
        )
    except (ValueError, OSError, RuntimeError) as exc:
        return enforce_budget({"ok": False, "kind": kind, "file": file, "error": str(exc)})

    counts = analysis.counts
    idle = [name for name in analysis.names if not counts[name]]
    unreferenced = [name for name in idle if name not in analysis.allowlisted]
    allowlisted = [name for name in idle if name in analysis.allowlisted]
    referenced = [name for name in analysis.names if counts[name]]
    page, truncated, _ = paginate(unreferenced, offset, limit)
    result: dict = {
        "ok": True,
        "kind": kind,
        "file": analysis.file,
        "total": len(analysis.names),
        "referenced": len(referenced),
        "unreferenced_total": len(unreferenced),
        "returned": len(page),
        "offset": offset,
        "limit": limit,
    }
    if allowlisted:
        allowlisted_page, more_allowlisted, _ = paginate(allowlisted, offset, limit)
        truncated = truncated or more_allowlisted
        result.update(allowlisted_total=len(allowlisted), allowlisted=allowlisted_page)
    if include_hits:
        hit_page, more_hits, _ = paginate(referenced, offset, limit)
        truncated = truncated or more_hits
        result["hits"] = [
            {"name": name, "count": counts[name], "samples": analysis.samples[name]}
            for name in hit_page
        ]
    result.update(truncated=truncated, unreferenced=page)
    result = enforce_budget(result, heavy_keys=("hits", "allowlisted", "unreferenced"))
    if "unreferenced_dropped" in result:
        result["returned"] = 0
    if any(f"{key}_dropped" in result for key in ("hits", "allowlisted", "unreferenced")):
        result["truncated"] = True
    return result


def _index_path(path: str) -> str:
    """Convert native index keys to the slash form used by the scan."""
    return Path(path).as_posix()


def analyse(
    mod_root: Path,
    kind: str,
    file: str,
    *,
    submod_root: Path | None,
    indexes: Mapping[str, GenericTxtIndex],
    samples: bool,
) -> Analysis:
    """Count references to every name `file` defines. Raises `ValueError` on bad input."""
    if kind not in KINDS:
        raise ValueError(f"Unknown kind {kind!r}. Use one of: {list(KINDS)}")
    roots = roots_for(mod_root, None, submod_root)
    if Path(file).is_absolute():
        raise ValueError(f"{file!r} must be a mod-relative path")
    path = validate_user_path(file, roots, extensions={".txt"}, require_file=True)
    root = next(r for r in roots if path.is_relative_to(r.resolve()))
    relpath = path.relative_to(root.resolve()).as_posix()

    definitions: frozenset[tuple[str, str, int | None]] = frozenset()
    if kind in _INDEXED:
        index = indexes[kind]
        # Index paths can use native Windows separators, including when restored
        # from an existing cache. Keep the original key for index lookups.
        errors = [e["error"] for e in index.parse_errors() if _index_path(e["file"]) == relpath]
        if errors:
            raise ValueError(f"{relpath} could not be parsed: {errors[0]}")
        indexed_files = index.list_files()
        names = list(
            dict.fromkeys(
                rec["id"]
                for indexed in indexed_files
                if _index_path(indexed) == relpath
                for rec in index.records_for_file(indexed)
            )
        )
        wanted = set(names)
        definitions = frozenset(
            (rec["id"], _index_path(indexed), rec["line"])
            for indexed in indexed_files
            for rec in index.records_for_file(indexed)
            if rec["id"] in wanted
        )
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        names = _idea_names(text) if kind == "idea" else _loc_names(text)

    counts = dict.fromkeys(names, 0)
    sampled: dict[str, list[dict]] = {name: [] for name in names}
    if names:
        name_set = frozenset(names)
        lookup = name_set | {f'"{name}"' for name in name_set}
        # Names the `\w+` scan cannot see (a hyphen, a colon) fall back to the registry pattern.
        odd = [
            (name, REFERENCE_PATTERNS[kind](name)) for name in names if not _WORD.fullmatch(name)
        ]
        for base, sub, rel, text in _walk(roots, _SPECS[kind]):
            if kind == "idea":
                hits = _idea_hits(text, name_set)
            elif kind == "scripted_loc":
                is_source = base == root and rel == relpath
                hits = _loc_hits(text, name_set, bracketed=sub == _LOC_DIR, is_source=is_source)
            else:
                hits = _call_hits(text, name_set, lookup, odd)
            for name, number, line in hits:
                if (name, rel, number) in definitions:
                    continue
                counts[name] += 1
                if samples and len(sampled[name]) < MAX_SAMPLES:
                    snippet = line.strip()[:SNIPPET_CHARS]
                    sampled[name].append({"file": rel, "line": number, "snippet": snippet})
    allowlisted = _allowlisted(mod_root, names, relpath) if kind in _INDEXED else frozenset()
    return Analysis(relpath, names, counts, sampled, allowlisted)


def _allowlisted(mod_root: Path, names: list[str], relpath: str) -> frozenset[str]:
    """Names `validation_config.json` marks as engine hooks or pending; none without the file."""
    try:
        config = json.loads((mod_root / "validation_config.json").read_text("utf-8"))[
            "validate_unused_scripted"
        ]
        patterns = [re.compile(p) for p in config.get("false_positive_patterns", ())]
    except (OSError, ValueError, KeyError, re.error):
        return frozenset()
    if Path(relpath).name in config.get("false_positive_files", ()):
        return frozenset(names)
    listed = {
        *config.get("false_positive_names", ()),
        *config.get("pending_implementation_names", ()),
    }
    return frozenset(n for n in names if n in listed or any(p.search(n) for p in patterns))


def _walk(
    roots: list[Path], specs: tuple[tuple[str, tuple[str, ...]], ...]
) -> Iterator[tuple[Path, str, str, str]]:
    """Yield `(root, scan dir, relpath, text)`; a path a higher-priority root has is skipped."""
    seen: set[str] = set()
    for base in roots:
        shadowed = frozenset(seen)
        for sub, suffixes in specs:
            directory = base / sub
            try:
                mode = directory.stat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                continue
            for suffix in suffixes:
                paths: list[Path] = []
                pending = [directory]
                while pending:
                    with os.scandir(pending.pop()) as entries:
                        for entry in entries:
                            path = Path(entry.path)
                            if path.relative_to(base).as_posix() in shadowed:
                                continue
                            # Explicit stat propagates classification and disappearance errors.
                            mode = entry.stat(follow_symlinks=False).st_mode
                            if stat.S_ISDIR(mode):
                                pending.append(path)
                            elif fnmatch.fnmatch(entry.name, f"*{suffix}"):
                                paths.append(path)
                for path in sorted(paths):
                    rel = path.relative_to(base).as_posix()
                    if rel in shadowed:
                        continue
                    if not stat.S_ISREG(path.stat().st_mode):
                        continue
                    seen.add(rel)
                    text = read_text(path)
                    if text is None:
                        raise OSError(f"Could not read scanned file {path}; analysis is incomplete")
                    yield base, sub, rel, text


def _idea_names(text: str) -> list[str]:
    names: dict[str, None] = {}
    depth = 0
    for line in text.splitlines():
        opens = line.count("{")
        closes = line.count("}")
        # Idea names sit at depth 2: ideas = { category = { idea_name = {
        if opens > closes and depth == 2:
            match = _IDEA_DEFINITION.match(line)
            if match and match[1] not in _IDEA_SKIP and not _TAG.match(match[1]):
                names[match[1]] = None
        depth += opens - closes
    return list(names)


def _loc_names(text: str) -> list[str]:
    return list(
        dict.fromkeys(m[1] for line in text.splitlines() if (m := _LOC_DEFINITION.match(line)))
    )


def _strip_inline_comment(line: str) -> str:
    """Port of upstream `shared_utils.strip_inline_comment`."""
    if "#" not in line:
        return line
    in_string = False
    for i, char in enumerate(line):
        if char == '"' and (i == 0 or line[i - 1] != "\\"):
            in_string = not in_string
        elif char == "#" and not in_string:
            return line[:i]
    return line


def _idea_hits(text: str, names: frozenset[str]) -> Iterator[Hit]:
    # Every idea pattern and block keyword contains "idea", so other lines cannot hit
    # and cannot open a block.
    if "idea" not in text:
        return
    depth = 0
    for number, line in enumerate(text.splitlines(), 1):
        in_block = depth > 0
        if not in_block and "idea" not in line:
            continue
        present = names.intersection(_WORD.findall(line))
        if present and (in_block or _IDEA_PATTERNS.search(line)):
            for name in present:
                yield name, number, line
        if in_block or _IDEA_BLOCK.search(line):
            code = _strip_inline_comment(line)
            depth = max(depth + code.count("{") - code.count("}"), 0)


def _loc_hits(
    text: str, names: frozenset[str], *, bracketed: bool, is_source: bool
) -> Iterator[Hit]:
    # `[name]` on a line implies the whole-word match upstream also requires there.
    find = (_BRACKETED if bracketed else _WORD).findall
    present = names.intersection(find(text))
    if not present:
        return
    for number, line in enumerate(text.splitlines(), 1):
        for name in present.intersection(find(line)):
            if is_source and re.fullmatch(rf"\s*name\s*=\s*{re.escape(name)}\s*", line):
                continue
            yield name, number, line


def _meta_candidates(text: str, names: frozenset[str]) -> Iterator[tuple[int, str]]:
    """Names a meta template can expand to, read as `scan_meta_constructed_names` reads them."""
    if "meta_effect" not in text and "meta_trigger" not in text:
        return
    code = _SCRIPT_TOKEN_RE.sub(
        lambda m: " " * len(m[0]) if m.lastgroup == "comment" else m[0], text
    )
    lowered = [(name, name.lower()) for name in names]
    for pattern in (_META_TEMPLATE, _QUOTED_META_TEMPLATE):
        for match in pattern.finditer(code):
            parts = _META_VARIABLE.split(match[1])
            prefix, suffix = parts[0].lower(), parts[-1].lower()
            if not prefix and not suffix:
                continue
            for name, lower in lowered:
                if (
                    lower.startswith(prefix)
                    and lower.endswith(suffix)
                    and len(lower) > len(prefix) + len(suffix)
                ):
                    yield match.start(1), name


def _call_hits(
    text: str,
    names: frozenset[str],
    lookup: frozenset[str],
    odd: list[tuple[str, re.Pattern[str]]],
) -> Iterator[Hit]:
    """Calls outside comments, and in a string only when quoted, plus tooltip and meta uses."""
    candidates = [(m.start(1), m[1].strip('"')) for m in _CALL.finditer(text) if m[1] in lookup]
    candidates.extend((m.start(1), m[1]) for m in _CUSTOM_TOOLTIP.finditer(text) if m[1] in names)
    for name, pattern in odd:
        candidates.extend((m.start(1), name) for m in pattern.finditer(text))
    tokens = _SCRIPT_TOKEN_RE.finditer(text) if candidates else ()
    spans = [(m.start(), m.end(), m.lastgroup == "comment") for m in tokens]
    span_starts = [start for start, _, _ in spans]
    active = list(_meta_candidates(text, names))
    for pos, name in candidates:
        i = bisect_right(span_starts, pos) - 1
        if i >= 0 and pos < spans[i][1]:
            start, end, is_comment = spans[i]
            if is_comment or text[start + 1 : end - 1] != name:
                continue
        active.append((pos, name))
    if not active:
        return
    starts = line_starts(text)
    seen: set[tuple[str, int]] = set()
    for pos, name in sorted(active):
        number = pos_to_line(pos, starts)
        if (name, number) in seen:
            continue
        seen.add((name, number))
        end_of_line = starts[number] if number < len(starts) else len(text)
        yield name, number, text[starts[number - 1] : end_of_line]
