"""Render an assembled focus tree the way the VSCode focus preview draws it.

Geometry follows MD-VSCode-Utility-Tool's focus preview: 96x130 px grid slots,
prerequisite lines that leave the parent's centre, turn half a row toward the
child and run into it, dashed lines for OR groups, and red lines between mutually
exclusive focuses. Offsets and conditional icons are decided by the mod's own
scenario evaluator, `tools/analysis/focus_overlap_report.py`, for the tree being
drawn and the chosen country, completed focuses, and date (see `focus_scenario`).

Output files go to `<cache_dir>/renders/`, never into the mod. PNG output and
icons need Pillow (the `render` extra); without it only HTML with placeholder
icons is available.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import math
import os
import re
import struct
import tempfile
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fnmatch import fnmatchcase
from functools import lru_cache, partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional

from ..paradox import ParseError, parse_string
from ..paradox.nodes import Node, node_line, symbol_or_str
from ..paradox.schema import focus_icon_choices
from ..util.git import GitError, run_git
from ..util.line_numbers import line_starts
from ..util.pathing import contained, roots_for
from ..util.response import coerce_int, enforce_budget, paginate
from .focus_scenario import (
    COUNTRY_RE,
    EVALUATOR,
    ScenarioEvaluator,
    check_completed,
    check_country,
    check_date,
)
from .focus_tree import (
    SupportsFilesForTag,
    assemble_tree_instances,
    available_shared_files,
    coordinate,
    layout_file,
    resolve_positions,
    scope_relpath,
)
from .scope import iter_scope_files

if TYPE_CHECKING:
    from ..indexes import GfxIndex, LocalisationIndex

SLOT_W = 96
SLOT_H = 130
CORNER = 0.5
ICON_LIFT = 18
LABEL_TOP = 85
LINE_HEIGHT = 15
FONT_SIZE = 12
# Three lines fill the slot below the icon. A label gets the room up to its row
# neighbours, but no more than the spare column a whole-tree view keeps.
LABEL_LINES = 3
LABEL_GAP = 8
LABEL_MAX_W = 3 * SLOT_W - LABEL_GAP
READABLE_SCALE = 0.75
HEADER_H = 28
PLACEHOLDER = 70
MAX_SIDE = 8192
MAX_PIXELS = 40_000_000
MAX_LIST = 500
MAX_NAME = 300
MAX_REGION = 1000
HTML_WARN_BYTES = 15_000_000
GIT_TIMEOUT = 30

BACKGROUND = "#1e1e1e"
TEXT = "#cccccc"
LINE = "#88aaff"
MUTEX = "#ff0000"
MISSING = "#808080"
HIGHLIGHT = "#ffcc33"

FORMATS = ("png", "html", "both")
LABELS = ("both", "name", "id", "none")
_REF_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./~^@{}-]*")
_LOC_CODES = re.compile(r"§[A-Za-z!]")
_SHARED_BLOCK = r"^[[:space:]]*(shared_focus|joint_focus)[[:space:]]*=[[:space:]]*\{"
# Uncompressed 32-bit DDS with these channel masks is BGRA; Pillow decodes it per pixel in Python.
_BGRA_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
_PNG_START = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"IEND\xaeB`\x82"
# Pillow's built-in font has ASCII glyphs only, so labels use a system font when one is found.
_FONT_FILES = ("Arial.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf")
_FONT_STACK = "Arial,'Liberation Sans','DejaVu Sans',system-ui,sans-serif"
_CHUNK = re.compile(r"[^_./-]*[_./-]+|[^_./-]+")


@dataclass
class _Item:
    id: str
    x: float
    y: float
    file: str
    line: Optional[int]
    icon: Optional[str]
    labels: list[str]
    highlighted: bool
    prerequisites: list[list[str]] = field(default_factory=list)
    mutually_exclusive: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def render_focus_tree(
    mod_root: Path,
    cache_dir: Path,
    focus_index: Optional[SupportsFilesForTag] = None,
    gfx_index: Optional[GfxIndex] = None,
    loc_index: Optional[LocalisationIndex] = None,
    *,
    tag: Optional[str] = None,
    file: Optional[str] = None,
    tree: Optional[str] = None,
    country: Optional[str] = None,
    completed: Optional[list] = None,
    date: Optional[str] = None,
    focus_ids: Optional[list] = None,
    region: Optional[list] = None,
    margin: int | float | str | None = 2,
    hide_branches: Optional[list] = None,
    highlight: Optional[list] = None,
    labels: str = "both",
    format: str = "png",
    scale: Optional[float] = None,
    ref: Optional[str] = None,
    vanilla_path: Optional[Path] = None,
    submod_root: Optional[Path] = None,
    limit: int | float | str | None = 20,
) -> dict:
    """Render one assembled focus tree to PNG and/or HTML under `cache_dir/renders`."""
    try:
        args = _validate(
            tag,
            file,
            tree,
            country,
            completed,
            date,
            focus_ids,
            region,
            margin,
            hide_branches,
            highlight,
            labels,
            format,
            scale,
            ref,
            limit,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    pil = _pillow()
    if args["format"] in ("png", "both") and pil is None:
        return {
            "ok": False,
            "error": "PNG output needs Pillow (pip install 'md-mcp[render]'); use format='html'.",
        }

    errors: list[dict] = []
    if file:
        candidates = [scope_relpath(file, mod_root, vanilla_path, submod_root)]
    elif tag:
        if focus_index is None:
            return {"ok": False, "error": "tag= scope requires the focus index."}
        candidates = list(focus_index.files_for_tag(tag))
    else:
        assert tree is not None
        candidates = _files_mentioning(tree, mod_root, vanilla_path, submod_root, r"\bid\s*=\s*")
    if not candidates:
        if tag and not tree:
            roots = _shared_roots_for_tag(
                tag, mod_root, vanilla_path, submod_root, focus_index, ref
            )
            return _shared_only(tag, roots, args, mod_root, vanilla_path, submod_root)
        return {"ok": False, "error": "No focus file matches the requested scope.", **_scope(args)}

    sources = _load(candidates, mod_root, vanilla_path, submod_root, ref, errors)
    if not sources:
        return enforce_budget(
            {
                "ok": False,
                "error": "Could not read or parse the focus files for this scope.",
                **_scope(args),
                "parse_errors": errors[:20],
            },
            heavy_keys=("parse_errors",),
        )
    layout_files = [layout_file(relpath, root, text) for relpath, root, text in sources]
    if any(t["shared_refs"] for source in layout_files for t in source["trees"]):
        loaded = set(candidates)
        extra = [
            relpath
            for relpath in _shared_files(
                mod_root, vanilla_path, submod_root, focus_index, ref, errors
            )
            if relpath not in loaded
        ]
        more = _load(extra, mod_root, vanilla_path, submod_root, ref, errors)
        sources.extend(more)
        layout_files.extend(layout_file(relpath, root, text) for relpath, root, text in more)
    focus_data, tree_tags = _render_data(sources)

    instances = assemble_tree_instances(layout_files)
    chosen, choices, why = _choose(instances, set(candidates), tag, tree)
    if chosen is None:
        if not choices and not tree and (tag or file):
            if file:
                roots = [
                    focus["id"]
                    for source in layout_files
                    if source["filepath"] in candidates
                    for focus in source["shared"]
                    if not focus["prerequisites"]
                ]
            else:
                assert tag is not None
                roots = _shared_roots_for_tag(
                    tag, mod_root, vanilla_path, submod_root, focus_index, ref
                )
            if roots:
                return _shared_only(
                    tag or file or "", roots, args, mod_root, vanilla_path, submod_root
                )
        result: dict = {"ok": False, "error": why, **_scope(args)}
        page, truncated, total = paginate(choices, 0, args["limit"])
        result.update(trees=page, trees_total=total, trees_truncated=truncated)
        if errors:
            result["parse_errors"] = errors[:20]
        return enforce_budget(result, heavy_keys=("trees", "parse_errors"))

    tree_file, tree_id, tree_line = chosen["key"]
    country_value = args["country"]
    country_source = "argument" if country_value else None
    if country_value is None:
        tags = tree_tags.get((tree_file, tree_line), [])
        if len(tags) == 1:
            country_value, country_source = tags[0], "tree"
        elif tag and COUNTRY_RE.fullmatch(tag) and len(tag) == 3:
            country_value, country_source = tag.upper(), "tag"

    evaluator = ScenarioEvaluator(
        mod_root,
        tree=tree_id,
        country=country_value,
        completed=args["completed"],
        date=args["date"],
    )
    texts = {relpath: text for relpath, _root, text in sources}
    applied: list[dict] = []
    unevaluated: list[dict] = []
    by_id: dict[str, list[dict]] = defaultdict(list)
    for focus in chosen["focuses"]:
        copy = dict(focus)
        parsed = focus_data.get((focus["file"], focus["line"], focus["id"]), {})
        for dx, dy, trigger in parsed.get("offsets", ()):
            # Upstream reads a missing trigger as an empty one, which holds.
            decided = (
                evaluator.decide("")
                if trigger is None
                else evaluator.decide_block(texts.get(focus["file"], ""), trigger)
            )
            if decided and copy["x"] is not None and copy["y"] is not None:
                copy["x"] = copy["x"] + dx
                copy["y"] = copy["y"] + dy
                applied.append({"focus": focus["id"], "x": float(dx), "y": float(dy)})
            elif decided is None:
                unevaluated.append({"focus": focus["id"], "x": float(dx), "y": float(dy)})
        by_id[focus["id"]].append(copy)
    positions, failures = resolve_positions(by_id)

    all_ids = set(by_id)
    warnings: list[str] = []
    if evaluator.error:
        warnings.append(f"offsets and conditional icons were not evaluated: {evaluator.error}")
    hidden = _expand(args["hide_branches"], all_ids, warnings, "hide_branches")
    hidden = _hide_descendants(hidden, by_id)
    highlighted = _expand(args["highlight"], all_ids, warnings, "highlight")
    resolved = {
        focus_id: (float(pos[0]), float(pos[1]))
        for focus_id, pos in positions.items()
        if pos is not None and focus_id in by_id and len(by_id[focus_id]) == 1
    }
    unresolved = sorted(all_ids - set(resolved))
    visible = {focus_id: xy for focus_id, xy in resolved.items() if focus_id not in hidden}
    if not visible:
        return enforce_budget(
            {
                "ok": False,
                "error": (
                    "hide_branches hid every focus in the tree."
                    if resolved
                    else "No focus in the tree resolves to a position."
                ),
                "tree": tree_id,
                "file": tree_file,
                "focuses": {
                    "in_tree": len(all_ids),
                    "hidden": len(hidden & set(resolved)),
                    "unresolved": len(unresolved),
                },
                "warnings": warnings[:20],
            },
            heavy_keys=("warnings",),
        )

    if args["region"] is not None:
        min_x, min_y, max_x, max_y = args["region"]
    else:
        if args["focus_ids"]:
            matched = _expand(args["focus_ids"], all_ids, warnings, "focus_ids")
            selected = matched & set(visible)
            if not selected:
                return enforce_budget(
                    {
                        "ok": False,
                        "error": "focus_ids matched no visible focus in the tree.",
                        "tree": tree_id,
                        "file": tree_file,
                        "matched_hidden": sorted(matched & hidden & set(resolved))[:10],
                        "matched_unresolved": sorted(matched - set(resolved))[:10],
                        "warnings": warnings[:20],
                    },
                    heavy_keys=("warnings",),
                )
            points = [visible[i] for i in selected]
            pad_x = pad_y = args["margin"]
        else:
            points = list(visible.values())
            # A spare column each side holds the labels of the outermost focuses.
            pad_x, pad_y = 1, 0
        min_x = math.floor(min(x for x, _ in points)) - pad_x
        max_x = math.ceil(max(x for x, _ in points)) + pad_x
        min_y = math.floor(min(y for _, y in points)) - pad_y
        max_y = math.ceil(max(y for _, y in points)) + pad_y

    items: list[_Item] = []
    for focus_id, (x, y) in sorted(visible.items(), key=lambda kv: (kv[1][1], kv[1][0], kv[0])):
        definition = by_id[focus_id][0]
        parsed = focus_data.get((definition["file"], definition["line"], focus_id), {})
        decide_icon = partial(evaluator.decide_block, texts.get(definition["file"], ""))
        items.append(
            _Item(
                id=focus_id,
                x=x,
                y=y,
                file=definition["file"],
                line=definition["line"],
                icon=_pick_icon(parsed.get("icons", ()), decide_icon),
                labels=_labels(focus_id, args["labels"], loc_index),
                highlighted=focus_id in highlighted,
                prerequisites=definition["prerequisites"],
                mutually_exclusive=definition["mutually_exclusive"],
            )
        )
    measure = _measure(pil)
    room = _label_room(visible)
    for item in items:
        item.lines = _label_lines(item.labels, room[item.id], measure)
    by_item = {item.id: item for item in items}
    in_view = [item for item in items if min_x <= item.x <= max_x and min_y <= item.y <= max_y]
    if not in_view:
        warnings.append("the region holds no visible focus")

    cols = max_x - min_x + 1
    rows = max_y - min_y + 1
    width_px = cols * SLOT_W
    height_px = rows * SLOT_H + HEADER_H
    # The caps bound an explicit scale too. Round down so the PNG never exceeds them.
    fit = min(MAX_SIDE / width_px, MAX_SIDE / height_px)
    fit = math.floor(min(fit, math.sqrt(MAX_PIXELS / (width_px * height_px))) * 1000) / 1000
    requested = 1.0 if args["scale"] is None else args["scale"]
    factor = min(requested, fit)
    if args["format"] in ("png", "both") and factor < 0.1:
        return {
            "ok": False,
            "error": (
                f"The view is {_num(cols)} x {_num(rows)} cells, too big for one PNG; "
                "crop with focus_ids or region, or use format='html'."
            ),
            "tree": tree_id,
            "file": tree_file,
        }
    if args["scale"] is not None and factor < requested:
        warnings.append(
            f"scale {requested:g} lowered to {factor:g} to keep the PNG within "
            f"{MAX_SIDE} px a side and {MAX_PIXELS} pixels"
        )
    if args["format"] in ("png", "both") and args["labels"] != "none" and factor < READABLE_SCALE:
        warnings.append(
            f"PNG labels are too small to read at scale {factor:g}; crop with focus_ids or "
            "region, or open the html page"
        )

    header = (
        f"{tree_id}  ({tree_file})  country: {country_value or 'none'}  "
        f"source: {ref or 'working tree'}  x {_num(min_x)}..{_num(max_x)}  "
        f"y {_num(min_y)}..{_num(max_y)}"
    )
    scene = _Scene(items=in_view, by_id=by_item, min_x=min_x, min_y=min_y, cols=cols, rows=rows)
    icons = _IconLoader(gfx_index, mod_root, vanilla_path, submod_root, cache_dir, pil)
    icons.no_icon = sum(item.icon is None for item in in_view)

    out_dir = cache_dir / "renders"
    stem = _stem(tree_id, country_value, ref, args)
    outputs: dict[str, str] = {}
    sizes: dict[str, int] = {}
    image_info: dict[str, Any] = {}
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if args["format"] in ("png", "both"):
            png_path = out_dir / f"{stem}.png"
            png_bytes, size = _render_png(scene, header, factor, icons, pil)
            _write_atomic(png_path, png_bytes)
            outputs["png"] = str(png_path)
            sizes["png"] = len(png_bytes)
            image_info = {"width": size[0], "height": size[1], "scale": factor}
        if args["format"] in ("html", "both"):
            html_path = out_dir / f"{stem}.html"
            html_bytes = _render_html(scene, header, icons).encode("utf-8")
            _write_atomic(html_path, html_bytes)
            outputs["html"] = str(html_path)
            sizes["html"] = len(html_bytes)
    except OSError as exc:
        return {
            "ok": False,
            "error": f"Cannot write renders under {out_dir}: {exc.strerror or exc}",
            "tree": tree_id,
            "file": tree_file,
        }
    if sizes.get("html", 0) > HTML_WARN_BYTES:
        warnings.append(
            f"the html page is {sizes['html'] / 1e6:.1f} MB; crop with focus_ids or region "
            "for a smaller page"
        )
    hint = (
        "Read the png path to view the image."
        if "html" not in outputs
        else "Open or share the html page; it is self-contained."
        if "png" not in outputs
        else "Read the png path to view the image; open or share the html page."
    )

    result = {
        "ok": True,
        "tree": {"id": tree_id, "file": tree_file, "line": tree_line},
        "country": country_value,
        "country_source": country_source,
        "scenario": {"completed": len(args["completed"]), "date": args["date"]},
        "source": ref or "working tree",
        "outputs": outputs,
        "bytes": sizes,
        "image": image_info or None,
        "region": {"min_x": min_x, "min_y": min_y, "max_x": max_x, "max_y": max_y},
        "focuses": {
            "in_tree": len(all_ids),
            "rendered": len(in_view),
            "hidden": len(hidden & set(resolved)),
            "unresolved": len(unresolved),
            "highlighted": len(highlighted),
        },
        "icons": icons.summary(),
        "offsets": {
            "evaluator": EVALUATOR if evaluator.error is None else None,
            "applied": len(applied),
            "unevaluated": len(unevaluated),
            "unevaluated_sample": unevaluated[:5],
        },
        "warnings": warnings[:20],
        "hint": hint,
    }
    if unresolved:
        result["unresolved_sample"] = unresolved[:10]
    if failures:
        result["chain_errors"] = len(failures)
    if errors:
        result["parse_errors"] = errors[:20]
    if len(choices) > 1:
        result["other_trees"] = [c for c in choices if c.get("tree") != tree_id][:10]
    return enforce_budget(result, heavy_keys=("parse_errors", "warnings", "other_trees"))


def _shared_roots_for_tag(
    tag: str,
    mod_root: Path,
    vanilla_path: Optional[Path],
    submod_root: Optional[Path],
    focus_index: Optional[SupportsFilesForTag],
    ref: Optional[str],
) -> list[str]:
    """Shared focuses without prerequisites whose id starts with `tag`."""
    errors: list[dict] = []
    return [
        focus["id"]
        for relpath, root, text in _load(
            _shared_files(mod_root, vanilla_path, submod_root, focus_index, ref, errors),
            mod_root,
            vanilla_path,
            submod_root,
            ref,
            errors,
        )
        for focus in layout_file(relpath, root, text)["shared"]
        if not focus["prerequisites"] and focus["id"].upper().startswith(tag.upper())
    ]


def _shared_files(
    mod_root: Path,
    vanilla_path: Optional[Path],
    submod_root: Optional[Path],
    focus_index: Optional[SupportsFilesForTag],
    ref: Optional[str],
    errors: list[dict],
) -> list[str]:
    """Files that may define shared or joint focuses, found at `ref` when one is given."""
    if ref is None:
        return available_shared_files(mod_root, vanilla_path, submod_root, focus_index)
    try:
        raw = run_git(
            ["grep", "-l", "-z", "-E", _SHARED_BLOCK, ref, "--", "common/national_focus"],
            cwd=mod_root,
            timeout=GIT_TIMEOUT,
        )
    except GitError as exc:
        # git grep exits 1 when no file matches.
        if exc.exit_code != 1:
            errors.append(
                {"file": "common/national_focus", "error": (exc.detail or str(exc))[:200]}
            )
        return []
    prefix = f"{ref}:"
    return [
        name[len(prefix) :] if name.startswith(prefix) else name
        for name in raw.decode("utf-8", "replace").split("\0")
        if name.endswith(".txt")
    ]


def _write_atomic(path: Path, data: bytes) -> None:
    """Write through a unique temp file and rename, so no reader sees a partial file."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp)
        raise


def _shared_only(
    label: str,
    roots: list[str],
    args: dict,
    mod_root: Path,
    vanilla_path: Optional[Path],
    submod_root: Optional[Path],
) -> dict:
    """Point a scope that holds only shared focuses at the tree files importing them."""
    files = _files_mentioning_any(
        roots, mod_root, vanilla_path, submod_root, r"\bshared_focus\s*=\s*"
    )
    if not files:
        return {"ok": False, "error": f"No focus_tree holds {label} focuses.", **_scope(args)}
    page, truncated, total = paginate([{"file": f} for f in files], 0, args["limit"])
    return enforce_budget(
        {
            "ok": False,
            "error": f"{label} holds shared focuses; pass file= or tree= of a tree importing them.",
            **_scope(args),
            "shared_roots": sorted(set(roots))[:20],
            "trees": page,
            "trees_total": total,
            "trees_truncated": truncated,
        },
        heavy_keys=("trees", "shared_roots"),
    )


def _validate(
    tag,
    file,
    tree,
    country,
    completed,
    date,
    focus_ids,
    region,
    margin,
    hide_branches,
    highlight,
    labels,
    format,
    scale,
    ref,
    limit,
) -> dict:
    if not (tag or file or tree):
        raise ValueError("Pass tree=, file= or tag= to choose a focus tree.")
    for name, value in (("tag", tag), ("file", file), ("tree", tree)):
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > MAX_NAME
        ):
            raise ValueError(f"{name} must be a non-empty string of at most {MAX_NAME} characters")
    country = None if country is None else check_country(country)
    date = check_date(date)
    completed = check_completed(completed)
    if format not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    if labels not in LABELS:
        raise ValueError(f"labels must be one of {', '.join(LABELS)}")
    if ref is not None and (not isinstance(ref, str) or not _REF_RE.fullmatch(ref) or ".." in ref):
        raise ValueError("ref must be a plain git revision such as HEAD or origin/main")
    lists: dict[str, list[str]] = {}
    for name, value in (
        ("focus_ids", focus_ids),
        ("hide_branches", hide_branches),
        ("highlight", highlight),
    ):
        if value is None:
            lists[name] = []
            continue
        if (
            not isinstance(value, list)
            or len(value) > MAX_LIST
            or any(not isinstance(v, str) or not v or len(v) > 200 for v in value)
        ):
            raise ValueError(f"{name} must be a list of at most {MAX_LIST} focus ids or globs")
        lists[name] = value
    box = None
    if region is not None:
        if not isinstance(region, list) or len(region) != 4:
            raise ValueError("region must be [min_x, min_y, max_x, max_y] in grid cells")
        try:
            box = [float(v) for v in region]
        except (TypeError, ValueError) as exc:
            raise ValueError("region values must be numbers") from exc
        if not all(math.isfinite(v) for v in box) or box[0] > box[2] or box[1] > box[3]:
            raise ValueError("region needs min_x <= max_x and min_y <= max_y")
        if box[2] - box[0] > MAX_REGION or box[3] - box[1] > MAX_REGION:
            raise ValueError(f"region spans at most {MAX_REGION} grid cells a side")
        box = [_num(v) for v in box]
    if scale is not None:
        if isinstance(scale, bool) or not isinstance(scale, (int, float)):
            raise ValueError("scale must be a number between 0.1 and 2")
        if not 0.1 <= float(scale) <= 2:
            raise ValueError("scale must be between 0.1 and 2")
    margin_value = coerce_int(margin, name="margin", default=2)
    if not 0 <= margin_value <= 50:
        raise ValueError("margin must be between 0 and 50 grid cells")
    limit_value = max(0, min(200, coerce_int(limit, name="limit", default=20)))
    return {
        "tag": tag,
        "file": file,
        "tree": tree,
        "country": country,
        "completed": completed,
        "date": date,
        "region": box,
        "margin": margin_value,
        "labels": labels,
        "format": format,
        "scale": None if scale is None else float(scale),
        "ref": ref,
        "limit": limit_value,
        **lists,
    }


def _scope(args: dict) -> dict:
    return {key: args[key] for key in ("tag", "file", "tree") if args[key]}


def _num(value: float) -> Any:
    return int(value) if float(value).is_integer() else value


def _pillow() -> Any:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    return (Image, ImageDraw, ImageFont)


def _focus_dirs(mod_root: Path, vanilla_path: Optional[Path], submod_root: Optional[Path]):
    for root in roots_for(mod_root, vanilla_path, submod_root):
        directory = root / "common" / "national_focus"
        if directory.is_dir():
            yield root, directory


def _files_mentioning(
    name: str,
    mod_root: Path,
    vanilla_path: Optional[Path],
    submod_root: Optional[Path],
    prefix: str,
) -> list[str]:
    return _files_mentioning_any([name], mod_root, vanilla_path, submod_root, prefix)


def _files_mentioning_any(
    names: Iterable[str],
    mod_root: Path,
    vanilla_path: Optional[Path],
    submod_root: Optional[Path],
    prefix: str,
) -> list[str]:
    wanted = sorted(set(names))
    if not wanted:
        return []
    pattern = re.compile(prefix + r"(?:" + "|".join(re.escape(n) for n in wanted) + r")\b")
    found: list[str] = []
    seen: set[str] = set()
    for root, directory in _focus_dirs(mod_root, vanilla_path, submod_root):
        for path in sorted(directory.rglob("*.txt")):
            relpath = path.relative_to(root).as_posix()
            if relpath in seen or not path.is_file():
                continue
            seen.add(relpath)
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            if pattern.search(text):
                found.append(relpath)
    return found


def _load(
    relpaths: list[str],
    mod_root: Path,
    vanilla_path: Optional[Path],
    submod_root: Optional[Path],
    ref: Optional[str],
    errors: list[dict],
) -> list[tuple[str, Node, str]]:
    if ref is None:
        return [
            (parsed.relpath, parsed.root, parsed.text)
            for parsed in iter_scope_files(
                relpaths, mod_root, vanilla_path, errors, submod_root=submod_root
            )
        ]
    loaded = []
    for relpath in relpaths:
        try:
            raw = run_git(["show", f"{ref}:./{relpath}"], cwd=mod_root, timeout=GIT_TIMEOUT)
        except GitError as exc:
            errors.append({"file": relpath, "error": (exc.detail or str(exc))[:200]})
            continue
        text = raw.decode("utf-8-sig", "replace")
        try:
            root = parse_string(text, relpath)
        except ParseError as exc:
            errors.append({"file": relpath, "error": str(exc)[:200]})
            continue
        loaded.append((relpath, root, text))
    return loaded


def _render_data(
    sources: list[tuple[str, Node, str]],
) -> tuple[dict[tuple, dict], dict[tuple, list[str]]]:
    """Offsets and icon choices per focus, plus the country tags each tree names."""
    data: dict[tuple, dict] = {}
    tree_tags: dict[tuple, list[str]] = {}
    for relpath, root, text in sources:
        starts = line_starts(text)
        for node in root.children():
            if node.name == "focus_tree":
                line = node_line(node, starts)
                country_block = node.get("country")
                tree_tags[(relpath, line)] = sorted(set(_country_tags(country_block)))
                for child in node.children():
                    if child.name == "focus":
                        _record(data, relpath, child, starts)
            elif node.name in ("shared_focus", "joint_focus"):
                _record(data, relpath, node, starts)
    return data, tree_tags


def _country_tags(node: Optional[Node]) -> Iterable[str]:
    if node is None:
        return
    for child in node.children():
        name = (child.name or "").lower()
        if name in ("tag", "original_tag"):
            value = symbol_or_str(child)
            if value and COUNTRY_RE.fullmatch(value):
                yield value.upper()
        elif isinstance(child.value, list):
            yield from _country_tags(child)


def _record(data: dict, relpath: str, node: Node, starts: list[int]) -> None:
    focus_id = symbol_or_str(node.get("id"))
    if not focus_id:
        return
    offsets = [
        (
            coordinate(block.get("x")) or Decimal(0),
            coordinate(block.get("y")) or Decimal(0),
            block.get("trigger"),
        )
        for block in node.get_all("offset")
        if isinstance(block.value, list)
    ]
    data[(relpath, node_line(node, starts), focus_id)] = {
        "offsets": offsets,
        "icons": focus_icon_choices(node),
    }


def _pick_icon(
    choices: Iterable[tuple[str, Optional[Node]]],
    decide: Callable[[Node], Optional[bool]],
) -> Optional[str]:
    """The first icon whose gate holds, else the first listed (the game would show none)."""
    choices = list(choices)
    for name, gate in choices:
        if gate is None or decide(gate) is True:
            return name
    return choices[0][0] if choices else None


def _choose(
    instances: list[dict], candidates: set[str], tag: Optional[str], tree: Optional[str]
) -> tuple[Optional[dict], list[dict], str]:
    own = [instance for instance in instances if instance["key"][0] in candidates]
    describe = [
        {"tree": inst["key"][1], "file": inst["key"][0], "line": inst["key"][2]} for inst in own
    ]
    if tree:
        matches = [inst for inst in own if inst["key"][1] == tree]
        if len(matches) == 1:
            return matches[0], describe, ""
        if not matches:
            return None, describe, f"No focus_tree with id {tree} in the matching files."
        return None, describe, f"Several files define focus_tree {tree}; pass file= as well."
    if tag:
        prefix = tag.upper() + "_"
        scored = [
            (sum(1 for f in inst["tree"]["focuses"] if f["id"].upper().startswith(prefix)), inst)
            for inst in own
        ]
        scored = [pair for pair in scored if pair[0] > 0]
        if scored:
            best = max(score for score, _ in scored)
            top = [inst for score, inst in scored if score == best]
            if len(top) == 1:
                return top[0], describe, ""
            return None, describe, f"Several trees hold {tag} focuses; pass tree=."
        return None, describe, f"No focus_tree holds {tag} focuses of its own."
    if len(own) == 1:
        return own[0], describe, ""
    if not own:
        return None, describe, "The file defines no focus_tree."
    return None, describe, "The file defines several focus trees; pass tree=."


def _expand(patterns: list[str], ids: set[str], warnings: list[str], name: str) -> set[str]:
    matched: set[str] = set()
    for pattern in patterns:
        hits = {focus_id for focus_id in ids if fnmatchcase(focus_id, pattern)}
        if not hits:
            warnings.append(f"{name}: {pattern} matched no focus in the tree")
        matched |= hits
    return matched


def _hide_descendants(hidden: set[str], by_id: dict[str, list[dict]]) -> set[str]:
    """A focus is hidden once every focus in one of its prerequisite groups is hidden."""
    hidden = set(hidden)
    changed = True
    while changed:
        changed = False
        for focus_id, definitions in by_id.items():
            if focus_id in hidden:
                continue
            for group in definitions[0]["prerequisites"]:
                present = [p for p in group if p in by_id]
                if present and all(p in hidden for p in present):
                    hidden.add(focus_id)
                    changed = True
                    break
    return hidden


def _labels(focus_id: str, mode: str, loc_index: Optional[LocalisationIndex]) -> list[str]:
    if mode == "none":
        return []
    if mode == "id":
        return [focus_id]
    name = None
    if loc_index is not None:
        record = loc_index.resolve(focus_id)
        if record and isinstance(record.get("value"), str):
            name = _LOC_CODES.sub("", record["value"]).replace("\\n", " ").strip() or None
    if mode == "name":
        return [name or focus_id]
    return [focus_id, name] if name and name != focus_id else [focus_id]


def _label_room(positions: dict[str, tuple[float, float]]) -> dict[str, float]:
    """Pixels each focus's label may take: up to the nearest focus either side in its row."""
    rows: dict[float, list[tuple[float, str]]] = defaultdict(list)
    for focus_id, (x, y) in positions.items():
        rows[y].append((x, focus_id))
    room: dict[str, float] = {}
    for row in rows.values():
        row.sort()
        for index, (x, focus_id) in enumerate(row):
            gaps = [abs(x - row[i][0]) for i in (index - 1, index + 1) if 0 <= i < len(row)]
            width = min(gaps) * SLOT_W - LABEL_GAP if gaps else LABEL_MAX_W
            room[focus_id] = max(SLOT_W - LABEL_GAP, min(LABEL_MAX_W, width))
    return room


def _label_lines(labels: list[str], width: float, measure: Callable[[str], float]) -> list[str]:
    """Wrap a focus's labels into at most LABEL_LINES lines, cutting the overflow with "..."."""
    wrapped = [_wrap(text, width, measure) for text in labels]
    if len(wrapped) != 2:
        return [line for text in wrapped for line in _cut(text, LABEL_LINES, width, measure)]
    focus_id, name = wrapped
    # The id is what a reader looks up, so it keeps up to two lines; the name gets the rest.
    id_lines = min(len(focus_id), LABEL_LINES - 1)
    name_lines = min(len(name), LABEL_LINES - id_lines)
    id_lines = min(len(focus_id), LABEL_LINES - name_lines)
    return _cut(focus_id, id_lines, width, measure) + _cut(name, name_lines, width, measure)


def _wrap(text: str, width: float, measure: Callable[[str], float]) -> list[str]:
    """Break at spaces, then after `_`, `.`, `/` or `-`, then anywhere."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        for index, chunk in enumerate(_CHUNK.findall(word)):
            candidate = line + (" " if index == 0 and line else "") + chunk
            if not line or measure(candidate) <= width:
                line = candidate
            else:
                lines.append(line)
                line = chunk
            while len(line) > 1 and measure(line) > width:
                cut = len(line) - 1
                while cut > 1 and measure(line[:cut]) > width:
                    cut -= 1
                lines.append(line[:cut])
                line = line[cut:]
    if line:
        lines.append(line)
    return lines


def _cut(lines: list[str], limit: int, width: float, measure: Callable[[str], float]) -> list[str]:
    if len(lines) <= limit:
        return lines
    if limit <= 0:
        return []
    last = lines[limit - 1]
    while last and measure(last + "...") > width:
        last = last[:-1]
    return [*lines[: limit - 1], last + "..."]


def _measure(pil: Any) -> Callable[[str], float]:
    """Width of a label line in unscaled pixels, measured with the PNG's font."""
    font = None if pil is None else _font(pil[2], FONT_SIZE)
    length = getattr(font, "getlength", None)
    if length is None:
        return lambda text: len(text) * 6.5
    return lru_cache(maxsize=None)(length)


def _stem(tree_id: str, country: Optional[str], ref: Optional[str], args: dict) -> str:
    base = f"{tree_id}-{country or 'none'}-{ref or 'worktree'}"
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base)[:80]
    digest = hashlib.sha1(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
    return f"{base}-{digest[:10]}"


@dataclass
class _Scene:
    items: list[_Item]
    by_id: dict[str, _Item]
    min_x: float
    min_y: float
    cols: float
    rows: float
    top: float = HEADER_H

    def origin(self, item: _Item) -> tuple[float, float]:
        """Unscaled top-left pixel of the item's slot."""
        return (item.x - self.min_x) * SLOT_W, (item.y - self.min_y) * SLOT_H + self.top

    def centre(self, item: _Item) -> tuple[float, float]:
        left, top = self.origin(item)
        return left + SLOT_W / 2, top + SLOT_H / 2

    def connections(self) -> Iterable[tuple[list[tuple[float, float, float, float]], str, bool]]:
        """(segments, colour, dashed) for every line touching an item in view."""
        drawn: set[tuple] = set()
        in_view = {item.id for item in self.items}
        for child in self.by_id.values():
            for group in child.prerequisites:
                dashed = len(group) > 1
                for parent_id in group:
                    parent = self.by_id.get(parent_id)
                    if parent is None or (child.id not in in_view and parent_id not in in_view):
                        continue
                    yield _segments(self.centre(parent), self.centre(child)), LINE, dashed
            for other_id in child.mutually_exclusive:
                other = self.by_id.get(other_id)
                key = tuple(sorted((child.id, other_id)))
                if other is None or key in drawn:
                    continue
                if child.id not in in_view and other_id not in in_view:
                    continue
                drawn.add(key)
                yield _segments(self.centre(child), self.centre(other)), MUTEX, False


def _segments(
    a: tuple[float, float], b: tuple[float, float]
) -> list[tuple[float, float, float, float]]:
    """The preview's line path from `a` to `b`: down from `a`, across, then into `b`."""
    ax, ay = a
    bx, by = b
    if ay == by or ax == bx:
        return [(ax, ay, bx, by)]
    corner = SLOT_H * CORNER
    dy = by - ay
    if abs(dy) < corner:
        return [(ax, ay, ax, by), (ax, by, bx, by)]
    turn = ay + math.copysign(corner, dy)
    return [(ax, ay, ax, turn), (ax, turn, bx, turn), (bx, turn, bx, by)]


class _IconLoader:
    """Resolves focus icon sprites to RGBA images, caching decoded PNGs by texture stat."""

    def __init__(
        self,
        gfx_index: Optional[GfxIndex],
        mod_root: Path,
        vanilla_path: Optional[Path],
        submod_root: Optional[Path],
        cache_dir: Path,
        pil: Any,
    ) -> None:
        self.gfx_index = gfx_index
        self.roots = roots_for(mod_root, vanilla_path, submod_root)
        self.cache = cache_dir / "renders" / "icons"
        self.pil = pil
        self._png: dict[str, Optional[bytes]] = {}
        self.missing: list[str] = []
        self.failed: list[str] = []
        self.no_icon = 0

    def png(self, name: Optional[str]) -> Optional[bytes]:
        if name is None or self.pil is None:
            return None
        if name not in self._png:
            self._png[name] = self._load(name)
        return self._png[name]

    def image(self, name: Optional[str]) -> Any:
        data = self.png(name)
        if data is None:
            return None
        image = self.pil[0].open(io.BytesIO(data))
        image.load()
        return image

    def summary(self) -> dict:
        resolved = sum(1 for value in self._png.values() if value is not None)
        return {
            "resolved": resolved,
            "no_icon": self.no_icon,
            "missing": len(self.missing),
            "failed": len(self.failed),
            "missing_sample": self.missing[:10],
            "failed_sample": self.failed[:10],
            "pillow": self.pil is not None,
        }

    def _load(self, name: str) -> Optional[bytes]:
        record = self.gfx_index.resolve(name) if self.gfx_index is not None else None
        if record is None and not name.startswith("GFX_") and self.gfx_index is not None:
            record = self.gfx_index.resolve("GFX_" + name)
        texture = (record or {}).get("texturefile")
        path = self._texture_path(texture) if isinstance(texture, str) else None
        if path is None:
            self.missing.append(name)
            return None
        try:
            stat = path.stat()
        except OSError as exc:
            self.failed.append(f"{name}: {type(exc).__name__}")
            return None
        key = hashlib.sha1(f"{path}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()
        cached = self.cache / f"{key}.png"
        with suppress(OSError):
            data = cached.read_bytes()
            # A damaged entry is decoded again and replaced.
            if data.startswith(_PNG_START) and data.endswith(_PNG_END):
                return data
        try:
            image = _decode_bgra_dds(self.pil[0], path)
            if image is None:
                with self.pil[0].open(path) as source:
                    source.load()
                    image = source.convert("RGBA")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        except Exception as exc:
            # Pillow raises NotImplementedError for some DDS formats, not only OSError.
            self.failed.append(f"{name}: {type(exc).__name__}")
            return None
        data = buffer.getvalue()
        with suppress(OSError):
            self.cache.mkdir(parents=True, exist_ok=True)
            _write_atomic(cached, data)
        return data

    def _texture_path(self, texture: str) -> Optional[Path]:
        names = [texture]
        lowered = texture.lower()
        if lowered.endswith(".dds"):
            names.append(texture[:-4] + ".tga")
        elif lowered.endswith(".tga"):
            names.append(texture[:-4] + ".dds")
        for root in self.roots:
            for candidate in names:
                path = contained(root, candidate)
                if path is not None and path.is_file():
                    return path
        return None


def _decode_bgra_dds(image_module: Any, path: Path) -> Any:
    """Decode an uncompressed 32-bit BGRA DDS in C, or return None for any other format."""
    with path.open("rb") as handle:
        header = handle.read(128)
        if len(header) < 128 or header[:4] != b"DDS ":
            return None
        height, width = struct.unpack_from("<2I", header, 12)
        flags, _fourcc, bits = struct.unpack_from("<3I", header, 80)
        # DDPF_RGB | DDPF_ALPHAPIXELS
        if flags & 0x41 != 0x41 or bits != 32:
            return None
        if struct.unpack_from("<4I", header, 92) != _BGRA_MASKS:
            return None
        size = width * height * 4
        data = handle.read(size)
    if not width or not height or len(data) != size:
        return None
    return image_module.frombuffer("RGBA", (width, height), data, "raw", "BGRA", 0, 1)


def _render_png(
    scene: _Scene, header: str, factor: float, icons: _IconLoader, pil: Any
) -> tuple[bytes, tuple[int, int]]:
    image_module, draw_module, font_module = pil
    width = max(1, round(scene.cols * SLOT_W * factor))
    height = max(1, round((scene.rows * SLOT_H + HEADER_H) * factor))
    canvas = image_module.new("RGBA", (width, height), BACKGROUND)
    draw = draw_module.Draw(canvas)
    stroke = max(1, round(factor))

    def px(value: float) -> float:
        return value * factor

    for segments, colour, dashed in scene.connections():
        for x1, y1, x2, y2 in segments:
            _line(draw, px(x1), px(y1), px(x2), px(y2), colour, stroke, dashed)

    for item in scene.items:
        left, top = scene.origin(item)
        icon = icons.image(item.icon)
        if icon is not None:
            natural_w, natural_h = icon.size
            w = max(1, round(natural_w * factor))
            h = max(1, round(natural_h * factor))
            if (w, h) != icon.size:
                icon = icon.resize((w, h), image_module.LANCZOS)
            x = round(px(left + SLOT_W / 2) - w / 2)
            y = round(px(top + (SLOT_H - natural_h) / 2 - ICON_LIFT))
            # The preview paints the icon as the slot's background, so clip it to the slot.
            box = (
                max(0, round(px(left)) - x),
                max(0, round(px(top)) - y),
                min(w, round(px(left + SLOT_W)) - x),
                min(h, round(px(top + SLOT_H)) - y),
            )
            if box[0] < box[2] and box[1] < box[3]:
                canvas.alpha_composite(icon.crop(box), (x + box[0], y + box[1]))
        else:
            cx, cy = px(left + SLOT_W / 2), px(top + SLOT_H / 2 - ICON_LIFT)
            half = px(PLACEHOLDER / 2)
            draw.rectangle((cx - half, cy - half, cx + half, cy + half), fill=MISSING)
        if item.highlighted:
            draw.rectangle(
                (px(left) + 2, px(top) + 2, px(left + SLOT_W) - 3, px(top + SLOT_H) - 3),
                outline=HIGHLIGHT,
                width=max(2, stroke + 1),
            )

    font = _font(font_module, max(7, round(FONT_SIZE * factor)))
    for item in scene.items:
        left, top = scene.origin(item)
        centre_x = px(left + SLOT_W / 2)
        for index, text in enumerate(item.lines):
            label_y = px(top + LABEL_TOP + index * LINE_HEIGHT)
            text_width = draw.textlength(text, font=font)
            draw.text((centre_x - text_width / 2, label_y), text, fill=TEXT, font=font)

    header_font = _font(font_module, max(9, round(14 * factor)))
    draw.rectangle((0, 0, width, px(HEADER_H) - 1), fill="#2d2d2d")
    draw.text((px(8), px(6)), header, fill=TEXT, font=header_font)
    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue(), (width, height)


def _font(font_module: Any, size: int) -> Any:
    path = _font_path(font_module)
    if path is not None:
        with suppress(OSError, ValueError):
            return font_module.truetype(path, size)
    try:
        return font_module.load_default(size=size)
    except TypeError:
        return font_module.load_default()


@lru_cache(maxsize=None)
def _font_path(font_module: Any) -> Optional[str]:
    """The first system font in _FONT_FILES that Pillow can find, looked up once."""
    for name in _FONT_FILES:
        with suppress(OSError, ValueError):
            return font_module.truetype(name, FONT_SIZE).path
    return None


def _line(
    draw: Any, x1: float, y1: float, x2: float, y2: float, colour: str, width: int, dashed: bool
) -> None:
    if not dashed:
        draw.line((x1, y1, x2, y2), fill=colour, width=width)
        return
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return
    dash = 3.0 * width
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    position = 0.0
    while position < length:
        end = min(position + dash, length)
        draw.line(
            (x1 + ux * position, y1 + uy * position, x1 + ux * end, y1 + uy * end),
            fill=colour,
            width=width,
        )
        position += dash * 2


def _render_html(scene: _Scene, header: str, icons: _IconLoader) -> str:
    # The page header is its own element, so the tree starts at the top of its box.
    scene = replace(scene, top=0)
    width = scene.cols * SLOT_W
    height = scene.rows * SLOT_H
    icon_classes: dict[str, str] = {}
    styles: list[str] = []
    parts: list[str] = []
    for segments, colour, dashed in scene.connections():
        border = f"1px {'dashed' if dashed else 'solid'} {colour}"
        for x1, y1, x2, y2 in segments:
            if y1 == y2:
                parts.append(
                    f'<div class="l" style="left:{min(x1, x2):g}px;top:{y1:g}px;'
                    f'width:{abs(x2 - x1):g}px;border-top:{border}"></div>'
                )
            else:
                parts.append(
                    f'<div class="l" style="left:{x1:g}px;top:{min(y1, y2):g}px;'
                    f'height:{abs(y2 - y1):g}px;border-left:{border}"></div>'
                )
    for item in scene.items:
        left, top = scene.origin(item)
        data = icons.png(item.icon)
        if data is not None and item.icon is not None:
            css = icon_classes.get(item.icon)
            if css is None:
                css = f"ic{len(icon_classes)}"
                icon_classes[item.icon] = css
                encoded = base64.b64encode(data).decode("ascii")
                styles.append(f".{css}{{background-image:url(data:image/png;base64,{encoded})}}")
            icon_html = f'<div class="i {css}"></div>'
        else:
            icon_html = '<div class="m"></div>'
        names = [text for text in item.labels if text != item.id]
        title = html.escape(
            "\n".join(
                [f"{item.id} ({_num(item.x)}, {_num(item.y)})", *names, f"{item.file}:{item.line}"]
            ),
            quote=True,
        )
        label = "<br>".join(html.escape(text) for text in item.lines)
        classes = "f hl" if item.highlighted else "f"
        parts.append(
            f'<div class="{classes}" style="left:{left:g}px;top:{top:g}px" title="{title}">'
            f'{icon_html}<span class="t">{label}</span></div>'
        )
    placeholder_left = (SLOT_W - PLACEHOLDER) / 2
    placeholder_top = SLOT_H / 2 - ICON_LIFT - PLACEHOLDER / 2
    base_css = "\n".join(
        [
            f"body{{margin:0;background:{BACKGROUND};color:{TEXT};"
            f"font:{FONT_SIZE}px/{LINE_HEIGHT}px {_FONT_STACK}}}",
            f".hdr{{position:sticky;top:0;left:0;z-index:10;height:{HEADER_H}px;"
            f"line-height:{HEADER_H}px;padding:0 8px;background:#2d2d2d;white-space:nowrap}}",
            f".tree{{position:relative;width:{width:g}px;height:{height:g}px;overflow:hidden}}",
            ".l{position:absolute;pointer-events:none}",
            f".f{{position:absolute;width:{SLOT_W}px;height:{SLOT_H}px;text-align:center}}",
            ".f .i{position:absolute;inset:0;background-repeat:no-repeat;"
            "background-position-x:center;"
            f"background-position-y:calc(50% - {ICON_LIFT}px);z-index:1}}",
            f".f .m{{position:absolute;left:{placeholder_left:g}px;top:{placeholder_top:g}px;"
            f"width:{PLACEHOLDER}px;height:{PLACEHOLDER}px;background:{MISSING};z-index:1}}",
            ".f .t{position:relative;z-index:3;display:inline-block;"
            f"margin:{LABEL_TOP}px -400px 0;white-space:nowrap}}",
            f".f.hl{{outline:2px solid {HIGHLIGHT};outline-offset:-3px;z-index:2}}",
            *styles,
        ]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(header.split("  ")[0])} focus tree</title>
<style>
{base_css}
</style>
</head>
<body>
<div class="hdr">{html.escape(header)}</div>
<div class="tree">
{"".join(parts)}
</div>
</body>
</html>
"""
