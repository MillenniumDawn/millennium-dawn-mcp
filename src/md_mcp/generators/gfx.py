"""GFX entry generator.

`generate_gfx_entry` scaffolds a single `spriteType` block. `generate_gfx_merge`
ports the merge semantics from `Millennium-Dawn/tools/gfx_entry_generator.py`:
unchanged entries stay byte-identical, texturefile changes replace in place,
new names are appended, orphans are reported and never deleted. Both return
strings. Neither writes a file.

Sprite naming is *not* ported. Upstream hardcodes a different rule per content
generator (`goals` uses bare stems, `event_pictures` prepends `GFX_`), so the
caller passes `prefix` to match the target file. There is no default.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Optional, Sequence

from ..util.encoding import read_text
from ..util.pathing import PathAccessError, validate_user_path
from ..util.response import enforce_budget, paginate

IMAGE_EXTENSIONS = {".dds", ".png", ".tga"}
SPRITETYPES_HEADER = "spriteTypes = {\n"


# Each scanner matches one anchored code pattern; the jump pattern finds the next
# place code can start and also matches the `#` and `"` that open a comment or
# string, so code never hides behind comment or string content. A backslash in a
# string takes the next character with it, and an unclosed string or last-line
# comment runs to the end. String bodies are consumed by _string_end with find
# hops, never a repeated regex: sre retains match state per repeat iteration, so
# megabyte escape-dense strings would otherwise peak near a gigabyte.
class _Scanner(NamedTuple):
    """An anchored code pattern plus the leftmost-scan pattern that finds it."""

    code: re.Pattern[str]
    jump: re.Pattern[str]


_BRACE_SCANNER = _Scanner(re.compile(r"([{}])"), re.compile(r'[#{}"]'))
_SPRITETYPE_SCANNER = _Scanner(
    re.compile(r"(?<!\w)([sS]priteType\s*=\s*\{)"), re.compile(r'[#"]|[sS]prite')
)
_NAME_SCANNER = _Scanner(
    re.compile(r'(?<!\w)name\s*=\s*"([^"]+)"'), re.compile(r'[#"]|(?<!\w)name')
)
_TEXTUREFILE_SCANNER = _Scanner(
    re.compile(r'(?<!\w)texture[fF]ile\s*=\s*"([^"]+)"'),
    re.compile(r'[#"]|(?<!\w)texture[fF]ile'),
)
# Real mod files also use lowercase spritetypes.
_SPRITE_TYPES_SCANNER = _Scanner(
    re.compile(r"(?<!\w)(spriteTypes\s*=\s*\{)", re.IGNORECASE),
    re.compile(r'[#"]|(?<!\w)spriteTypes', re.IGNORECASE),
)
# Where a comment or string can open; both scanners and _gfx_error hop on it.
_SKIP_OPENERS = re.compile(r'[#"]')
_HIDDEN_BLOCK_RE = re.compile(r"(?<!\w)spriteTypes?\s*=\s*\{", re.IGNORECASE)
_NO_SPRITE_TYPES_ROOT = "gfx_file has no spriteTypes block"
_UNTERMINATED_STRING = "gfx_file has an unterminated string at line {}"
_STRING_HIDES_BLOCK = "gfx_file has a multi-line string that hides a sprite block at line {}"

_Render = Callable[[str, str], str]


def generate_gfx_entry(
    *,
    name: str,
    texturefile: str,
    kind: str = "spriteType",
    frames: Optional[int] = None,
    legacy_lazy_load: bool = False,
) -> dict:
    """Scaffold a single GFX sprite entry.

    Args:
      name             — sprite name (e.g. `GFX_focus_my_focus`); the convention is
                         to use a `GFX_` prefix, but the engine doesn't require it
      texturefile      — DDS/TGA path, mod-relative
      kind             — `spriteType` (default), `corneredTileSpriteType`,
                         `frameAnimatedSpriteType`, ...
      frames           — `noOfFrames = N` for animated/frame strips
      legacy_lazy_load — older `legacyLazyLoad = yes` flag (rare; some mods use it
                         for very large sprites)

    Returns: `{txt: str}` ready to slot inside an existing `spriteTypes = { }` block.
    """
    return enforce_budget(
        {
            "txt": _render_entry(
                name,
                texturefile,
                kind=kind,
                frames=frames,
                legacy_lazy_load=legacy_lazy_load,
            )
        },
        heavy_keys=("txt",),
    )


def generate_gfx_merge(
    mod_root: Path,
    *,
    texture_dir: str,
    gfx_file: str,
    prefix: str,
    kind: str = "spriteType",
    frames: Optional[int] = None,
    legacy_lazy_load: bool = False,
    protected: Optional[Sequence[str]] = None,
    limit: int = 100,
    offset: int = 0,
    include_file: bool = False,
) -> dict:
    """Merge a texture directory into an existing `.gfx` file without writing.

    Scans `texture_dir` for `.dds`/`.png`/`.tga`, names each `prefix + stem`
    (or the stem alone when it already starts with `prefix`), and merges into
    `gfx_file` using the same merge rules as the mod's `gfx_entry_generator.py`.
    `prefix` has to match the target file's convention: `goals.gfx` stores bare
    stems (`prefix=""`), `MD_eventpictures.gfx` stores `GFX_`-prefixed ones.

    `txt` covers exactly the returned `new` slice, so `limit`/`offset` bound it
    along with the name lists. When `gfx_file` is missing or empty, `txt` is a
    full `spriteTypes` document: page 0 carries the header, the last page the
    closing brace, so concatenating the pages in order rebuilds the file. Pass
    `include_file=True` to also get `file_txt` (the complete merged document);
    real MD files like `goals.gfx` will trip `enforce_budget` and drop it.
    A nonempty file with no closed `spriteTypes` root returns
    `{ok: false, error: "gfx_file has no spriteTypes block"}`. A string that never
    closes, anywhere in the file, returns
    `{ok: false, error: "gfx_file has an unterminated string at line N"}`. N is the
    line where the first string that crosses a line opens, or where the unclosed
    one opens if none does. That is the stray quote unless a valid multi-line
    string comes earlier. Two stray quotes can instead pair up around a
    `spriteType` or `spriteTypes` block. The error is then "gfx_file has a
    multi-line string that hides a sprite block at line N", where N is the line
    where that string opens.

    Returns `{ok, txt, new, changed, orphaned, ...}`. Never writes.
    """
    extra = {"texture_dir": texture_dir, "gfx_file": gfx_file}
    if not str(texture_dir).strip():
        return {"ok": False, "error": "texture_dir is required", **extra}
    if not str(gfx_file).strip():
        return {"ok": False, "error": "gfx_file is required", **extra}

    try:
        root = mod_root.resolve()
        tex_root = _resolve_under_mod(root, texture_dir, label="texture_dir")
        gfx_path = _resolve_under_mod(root, gfx_file, label="gfx_file")
    except ValueError as e:
        return {"ok": False, "error": str(e), **extra}
    if not tex_root.is_dir():
        return {"ok": False, "error": f"texture_dir is not a directory: {texture_dir}", **extra}

    entries, scan_duplicates = _scan_entries(root, tex_root, prefix)
    exists = gfx_path.is_file()
    original = read_text(gfx_path) if exists else ""
    if not original.strip():
        original = ""
    text_error = _gfx_error(original)
    if text_error:
        return {"ok": False, "error": text_error, **extra}

    def render(name: str, texture_path: str) -> str:
        return (
            _render_entry(
                name,
                texture_path,
                kind=kind,
                frames=frames,
                legacy_lazy_load=legacy_lazy_load,
            )
            + "\n"
        )

    merged = merge_gfx_text(
        original,
        entries,
        render,
        protected=frozenset(protected or ()),
    )

    new_names: list[str] = merged["new"]
    changed_rows = [
        {
            "name": name,
            "old_texturefile": old,
            "texturefile": entries[name],
            "txt": render(name, entries[name]).rstrip("\n"),
        }
        for name, old in merged["changed"]
    ]
    new_s, new_trunc, new_total = paginate(new_names, offset, limit)
    ch_s, ch_trunc, ch_total = paginate(changed_rows, offset, limit)
    or_s, or_trunc, or_total = paginate(merged["orphaned"], offset, limit)
    du_s, du_trunc, du_total = paginate(merged["deduped"], offset, limit)
    cf_s, cf_trunc, conflict_total = paginate(merged["conflicts"], offset, limit)
    sd_s, sd_trunc, sd_total = paginate(scan_duplicates, offset, limit)

    txt = "".join(render(name, entries[name]) for name in new_s)
    if not original and new_names:
        if offset <= 0:
            txt = SPRITETYPES_HEADER + txt
        if not new_trunc:
            txt += "}\n"

    result = {
        "ok": True,
        "gfx_file": gfx_file,
        "exists": exists,
        "scanned": len(entries) + len(scan_duplicates),
        "would_write": merged["would_write"],
        "txt": txt,
        "new_total": new_total,
        "changed_total": ch_total,
        "orphaned_total": or_total,
        "deduped_total": du_total,
        "conflict_total": conflict_total,
        "scan_duplicate_total": sd_total,
        "new": new_s,
        "changed": ch_s,
        "orphaned": or_s,
        "deduped": du_s,
        "conflicts": cf_s,
        "scan_duplicates": sd_s,
        "truncated": new_trunc or ch_trunc or or_trunc or du_trunc or cf_trunc or sd_trunc,
        "returned": len(new_s) + len(ch_s) + len(or_s) + len(du_s) + len(cf_s) + len(sd_s),
    }
    if include_file:
        result["file_txt"] = merged["txt"]
    return enforce_budget(
        result,
        heavy_keys=(
            "file_txt",
            "changed",
            "scan_duplicates",
            "orphaned",
            "deduped",
            "conflicts",
            "new",
            "txt",
        ),
    )


def merge_gfx_text(
    original: str,
    entries: dict[str, str],
    render: _Render,
    *,
    header: str = SPRITETYPES_HEADER,
    protected: frozenset[str] = frozenset(),
) -> dict:
    """Merge `name -> texturefile` entries into an existing spriteTypes document.

    Pure function: returns the merged text and the same report lists as
    `merge_gfx_entries` in the mod tool. Does not touch the filesystem. An
    empty or whitespace-only `original` means "no document yet" and gets a
    `header`/`}` wrapper. Otherwise ValueError is raised, with the messages and
    line rule of `generate_gfx_merge`, for a document with no closed spriteTypes
    root, a string that never closes, or a multi-line string that hides a sprite
    block.

    `changed` is a list of `(name, old_texturefile)` pairs. `conflicts` is a
    list of `{name, kept, dropped}` dicts for de-duplicated blocks whose
    texturefiles disagreed.
    """
    normalized = original.replace("\r\n", "\n").replace("\r", "\n") if original else ""
    original = normalized if normalized.strip() else f"{header}}}\n"
    text_error = _gfx_error(original)
    if text_error:
        raise ValueError(text_error)

    existing: dict[str, tuple[Optional[str], int, int]] = {}
    dup_spans: list[tuple[int, int]] = []
    deduped_names: list[str] = []
    conflicts: list[dict[str, str]] = []
    for name, texfile, start, end in _parse_named_blocks(original):
        if not name:
            continue
        if name not in existing:
            existing[name] = (texfile, start, end)
            continue
        kept_texfile = existing[name][0]
        if texfile and kept_texfile and texfile != kept_texfile:
            conflicts.append({"name": name, "kept": kept_texfile, "dropped": texfile})
        # Remove only this duplicate block.  Deleting its whole line erases
        # neighboring entries when two blocks share a line.
        span_end = end
        while span_end < len(original) and original[span_end] in " \t":
            span_end += 1
        if span_end < len(original) and original[span_end] == "#":
            line_end = original.find("\n", span_end)
            span_end = line_end if line_end != -1 else len(original)
        dup_spans.append((start, span_end))
        deduped_names.append(name)

    new_names: list[str] = []
    changed: list[tuple[str, Optional[str]]] = []
    splices: list[tuple[int, int, str]] = [(ls, se, "") for ls, se in dup_spans]
    for name in sorted(entries, key=lambda n: entries[n].lower()):
        texture_path = entries[name]
        if name in existing and name not in protected:
            old_texfile, start, end = existing[name]
            if old_texfile != texture_path:
                block = render(name, texture_path)
                core = block[1:] if block.startswith("\t") else block
                splices.append((start, end, core.rstrip("\n")))
                changed.append((name, old_texfile))
        elif name in existing:
            pass
        else:
            new_names.append(name)

    orphaned = sorted(set(existing) - set(entries) - set(protected))

    if new_names:
        insert_at = _sprite_types_close(original)
        appended = "".join(render(name, entries[name]) for name in new_names)
        splices.append((insert_at, insert_at, appended))

    if splices:
        splices.sort(key=lambda s: s[0])
        pieces: list[str] = []
        cursor = 0
        for start, end, replacement in splices:
            pieces.append(original[cursor:start])
            pieces.append(replacement)
            cursor = end
        pieces.append(original[cursor:])
        text = "".join(pieces)
    else:
        text = original

    return {
        "txt": text,
        "new": new_names,
        "changed": changed,
        "orphaned": orphaned,
        "deduped": sorted(set(deduped_names)),
        "conflicts": conflicts,
        "would_write": text != original,
    }


def _render_entry(
    name: str,
    texturefile: str,
    *,
    kind: str = "spriteType",
    frames: Optional[int] = None,
    legacy_lazy_load: bool = False,
) -> str:
    parts = [f"\t{kind} = {{"]
    parts.append(f'\t\tname = "{name}"')
    parts.append(f'\t\ttexturefile = "{texturefile}"')
    if frames is not None:
        parts.append(f"\t\tnoOfFrames = {frames}")
    if legacy_lazy_load:
        parts.append("\t\tlegacyLazyLoad = yes")
    parts.append("\t}")
    return "\n".join(parts)


def _scan_entries(
    mod_root: Path, tex_root: Path, prefix: str
) -> tuple[dict[str, str], list[dict[str, str]]]:
    files = [p for p in tex_root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    files.sort(key=lambda p: p.as_posix().lower())
    entries: dict[str, str] = {}
    duplicates: list[dict[str, str]] = []
    for f in files:
        texture_path = f.relative_to(mod_root).as_posix()
        stem = f.stem
        name = stem if prefix and stem.startswith(prefix) else f"{prefix}{stem}"
        if name in entries:
            duplicates.append({"name": name, "texturefile": texture_path})
            continue
        entries[name] = texture_path
    return entries, duplicates


def _resolve_under_mod(mod_root: Path, rel: str, *, label: str) -> Path:
    try:
        return validate_user_path(rel, [mod_root])
    except PathAccessError:
        raise ValueError(f"{label} escapes mod root: {rel}") from None


def _string_end(text: str, start: int, stop: int) -> tuple[int, bool]:
    r"""Consume the string opening at `start`; return (end, closed). The quote
    closes iff the backslash run before it is even (a backslash takes the next
    character, including newline and quote)."""
    i = start + 1
    while True:
        quote = text.find('"', i, stop)
        if quote == -1:
            return stop, False
        run_end = quote - 1
        while run_end >= start and text[run_end] == "\\":
            run_end -= 1
        if (quote - 1 - run_end) % 2 == 1:
            i = quote + 1  # odd backslash run: the quote is escaped
        else:
            return quote + 1, True


def _code_matches(
    scanner: _Scanner, text: str, start: int = 0, end: Optional[int] = None
) -> Iterator[re.Match[str]]:
    """Yield the scanner's code matches that fall outside comments and strings."""
    stop = len(text) if end is None else end
    i = start
    while i < stop:
        landing = scanner.jump.search(text, i, stop)
        if landing is None:
            return
        pos = landing.start()
        opener = text[pos]
        if opener == "#":
            newline = text.find("\n", pos, stop)
            i = stop if newline == -1 else newline + 1
        elif opener == '"':
            i, _ = _string_end(text, pos, stop)
        else:
            match = scanner.code.match(text, pos, stop)
            if match is None:
                i = pos + 1
            else:
                yield match
                i = match.end()


def _match_brace(text: str, open_idx: int) -> int:
    """Return the brace that closes the one at `open_idx`, or -1."""
    depth = 0
    for m in _code_matches(_BRACE_SCANNER, text, open_idx):
        depth += 1 if m.group(1) == "{" else -1
        if depth == 0:
            return m.start()
    return -1


def _parse_named_blocks(text: str):
    """Yield `(name, texturefile, start, end)` for every closed spriteType block."""
    for m in _code_matches(_SPRITETYPE_SCANNER, text):
        close = _match_brace(text, m.end() - 1)
        if close == -1:
            continue
        name = next(_code_matches(_NAME_SCANNER, text, m.end(), close), None)
        texture = next(_code_matches(_TEXTUREFILE_SCANNER, text, m.end(), close), None)
        yield name and name.group(1), texture and texture.group(1), m.start(), close + 1


def _sprite_types_close(text: str) -> int:
    """Return the closing brace of the outer spriteTypes block, or -1."""
    root = next(_code_matches(_SPRITE_TYPES_SCANNER, text), None)
    return -1 if root is None else _match_brace(text, root.end() - 1)


def _line_at(text: str, pos: int) -> int:
    # count, not line_starts: a 10 MB file of short lines would build a huge list.
    return text.count("\n", 0, pos) + 1


def _gfx_error(text: str) -> Optional[str]:
    """Return why `text` cannot be merged into, or None.

    Strings are paired left to right, so one stray quote flips every pairing after
    it. For an unterminated string the line is where the first string that crosses
    a line opens: the stray quote, unless a valid multi-line string comes first
    (rare in gfx files). For a hidden block it is where the hiding string opens.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return None
    crossing: Optional[int] = None
    hider: Optional[int] = None
    tail: Optional[int] = None
    length = len(text)
    i = 0
    while i < length:
        landing = _SKIP_OPENERS.search(text, i)
        if landing is None:
            break
        pos = landing.start()
        if text[pos] == "#":
            newline = text.find("\n", pos)
            i = length if newline == -1 else newline + 1
            continue
        end, closed = _string_end(text, pos, length)
        if not closed:
            tail = pos
            break  # the string consumed the rest; no later string can exist
        if text.find("\n", pos, end) != -1:
            if crossing is None:
                crossing = pos
            if hider is None and _HIDDEN_BLOCK_RE.search(text, pos, end):
                hider = pos
        i = end
    if tail is not None:
        return _UNTERMINATED_STRING.format(_line_at(text, tail if crossing is None else crossing))
    if hider is not None:
        return _STRING_HIDES_BLOCK.format(_line_at(text, hider))
    return _NO_SPRITE_TYPES_ROOT if _sprite_types_close(text) == -1 else None
