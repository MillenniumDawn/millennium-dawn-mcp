"""GFX entry generator.

`generate_gfx_entry` scaffolds a single `spriteType` block. `generate_gfx_merge`
ports the merge semantics from `Millennium-Dawn/tools/gfx_entry_generator.py`:
unchanged entries stay byte-identical, texturefile changes replace in place,
new names are appended, orphans are reported and never deleted. Both return
strings. Neither writes a file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..util.encoding import read_text
from ..util.response import enforce_budget, paginate

IMAGE_EXTENSIONS = {".dds", ".png", ".tga"}

_SPRITETYPE_RE = re.compile(r"[sS]priteType\s*=\s*\{")
_NAME_RE = re.compile(r'name\s*=\s*"([^"]+)"')
_TEXTUREFILE_RE = re.compile(r'texture[fF]ile\s*=\s*"([^"]+)"')

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
    return {
        "txt": _render_entry(
            name,
            texturefile,
            kind=kind,
            frames=frames,
            legacy_lazy_load=legacy_lazy_load,
        )
    }


def generate_gfx_merge(
    mod_root: Path,
    *,
    texture_dir: str,
    gfx_file: str,
    prefix: str = "GFX_",
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
    `gfx_file` using the same rules as the mod's `gfx_entry_generator.py`.

    `txt` is the new sprite blocks to append (or a full `spriteTypes` file when
    `gfx_file` does not exist yet). Name lists are paginated. Pass
    `include_file=True` to also get `file_txt` (the complete merged document);
    real MD files like `goals.gfx` will trip `enforce_budget` and drop it.

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
    if exists:
        txt = "".join(render(name, entries[name]) for name in new_names)
    elif new_names:
        txt = merged["txt"]
    else:
        txt = ""

    new_s, new_trunc, new_total = paginate(new_names, offset, limit)
    ch_s, ch_trunc, ch_total = paginate(changed_rows, offset, limit)
    or_s, or_trunc, or_total = paginate(merged["orphaned"], offset, limit)
    du_s, du_trunc, du_total = paginate(merged["deduped"], offset, limit)
    cf_s, cf_trunc, conflict_total = paginate(merged["conflicts"], offset, limit)

    result = {
        "ok": True,
        "gfx_file": gfx_file,
        "exists": exists,
        "scanned": len(entries) + len(scan_duplicates),
        "would_write": merged["would_write"] if exists else bool(new_names),
        "txt": txt,
        "new_total": new_total,
        "changed_total": ch_total,
        "orphaned_total": or_total,
        "deduped_total": du_total,
        "conflict_total": conflict_total,
        "new": new_s,
        "changed": ch_s,
        "orphaned": or_s,
        "deduped": du_s,
        "conflicts": cf_s,
        "scan_duplicates": scan_duplicates,
        "truncated": new_trunc or ch_trunc or or_trunc or du_trunc or cf_trunc,
        "returned": len(new_s) + len(ch_s) + len(or_s) + len(du_s),
    }
    if include_file:
        result["file_txt"] = merged["txt"]
    return enforce_budget(
        result,
        heavy_keys=("file_txt", "txt", "changed", "new", "orphaned", "deduped", "conflicts"),
    )


def merge_gfx_text(
    original: str,
    entries: dict[str, str],
    render: _Render,
    *,
    header: str = "spriteTypes = {\n",
    protected: frozenset[str] = frozenset(),
) -> dict:
    """Merge `name -> texturefile` entries into an existing spriteTypes document.

    Pure function: returns the merged text and the same report lists as
    `merge_gfx_entries` in the mod tool. Does not touch the filesystem.

    `changed` is a list of `(name, old_texturefile)` pairs. `conflicts` is a
    list of `{name, kept, dropped}` dicts for de-duplicated blocks whose
    texturefiles disagreed.
    """
    original = original.replace("\r\n", "\n").replace("\r", "\n") if original else f"{header}}}\n"

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
        line_start = original.rfind("\n", 0, start) + 1
        line_end = original.find("\n", end)
        span_end = line_end + 1 if line_end != -1 else len(original)
        dup_spans.append((line_start, span_end))
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

    if new_names:
        appended = "".join(render(name, entries[name]) for name in new_names)
        insert_at = text.rfind("}")
        if insert_at == -1:
            text = text + appended
        else:
            text = text[:insert_at] + appended + text[insert_at:]

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
    raw = Path(rel)
    p = raw.resolve() if raw.is_absolute() else (mod_root / raw).resolve()
    if not p.is_relative_to(mod_root):
        raise ValueError(f"{label} escapes mod root: {rel}")
    return p


def _match_brace(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"Unmatched opening brace at offset {open_idx}")


def _parse_named_blocks(text: str):
    """Yield `(name, texturefile, start, end)` for every spriteType block."""
    for m in _SPRITETYPE_RE.finditer(text):
        open_idx = m.end() - 1
        try:
            end = _match_brace(text, open_idx) + 1
        except ValueError:
            continue
        nm = _NAME_RE.search(text, m.start(), end)
        tx = _TEXTUREFILE_RE.search(text, m.start(), end)
        yield nm.group(1) if nm else None, tx.group(1) if tx else None, m.start(), end
