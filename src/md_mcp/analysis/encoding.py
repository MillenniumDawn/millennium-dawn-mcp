"""Encoding compliance check.

Per `Millennium-Dawn/.claude/rules/general-rules.md`:
  * `.txt` files must be UTF-8 **without** BOM (`EF BB BF`)
  * `.yml` files in `localisation/` must be UTF-8 **with** BOM

Walks the configured scan paths and reports every violation with the actual vs.
expected state. Returns `{ok, violations: [...], counts}`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..util.response import coerce_int, enforce_budget, paginate

UTF8_BOM = b"\xef\xbb\xbf"

# Subdirectories to walk. Other paths (resources/, .claude/, tools/) are excluded
# because they're not subject to the engine's encoding rules.
_TXT_DIRS = [
    "common",
    "events",
    "history",
    "interface",
]
_YML_DIRS = [
    "localisation",
]


def check_encoding(
    mod_root: Path,
    *,
    files: Optional[list[str]] = None,
    submod_root: Optional[Path] = None,
    limit: int | float | str | None = 200,
    offset: int | float | str | None = 0,
) -> dict:
    """Verify BOM rules across the mod (or a specific file list).

    Args:
      files — explicit mod-relative paths to check; if omitted, walks the standard dirs
      limit — maximum violations to return
      offset — number of violations to skip before returning the page

    Returns: `{ok, checked, total, returned, truncated, violations, counts}` where
        violations is [{file, expected: "no-bom" | "bom", actual: "no-bom" | "bom"}, ...]
    """
    try:
        limit = coerce_int(limit, name="limit", default=200)
        offset = coerce_int(offset, name="offset", default=0)
    except ValueError as exc:
        return enforce_budget({"ok": False, "error": str(exc)})

    roots = [root for root in (submod_root, mod_root) if root is not None]
    targets: list[Path] = []
    target_relpaths: dict[Path, str] = {}
    seen_relpaths: set[str] = set()
    if files:
        for f in files:
            path = Path(f)
            rel = path.as_posix() if not path.is_absolute() else str(path)
            candidates = [path] if path.is_absolute() else [root / path for root in roots]
            for candidate in candidates:
                if candidate.exists():
                    targets.append(candidate)
                    target_relpaths[candidate] = rel
                    break
    else:
        for root in roots:
            for sub in (*_TXT_DIRS, *_YML_DIRS):
                suffix = "*.yml" if sub in _YML_DIRS else "*.txt"
                d = root / sub
                if not d.is_dir():
                    continue
                for path in d.rglob(suffix):
                    if not path.is_file():
                        continue
                    rel = str(path.relative_to(root))
                    if rel in seen_relpaths:
                        continue
                    seen_relpaths.add(rel)
                    targets.append(path)
                    target_relpaths[path] = rel

    violations: list[dict] = []
    for path in targets:
        try:
            with open(path, "rb") as fh:
                head = fh.read(3)
        except OSError:
            continue

        has_bom = head == UTF8_BOM
        resolved_rel = target_relpaths.get(path)
        if resolved_rel is None:
            for root in roots:
                try:
                    resolved_rel = str(path.relative_to(root))
                    break
                except ValueError:
                    continue
            if resolved_rel is None:
                resolved_rel = str(path)

        if path.suffix.lower() == ".txt" and has_bom:
            violations.append({"file": resolved_rel, "expected": "no-bom", "actual": "bom"})
        elif (
            path.suffix.lower() == ".yml"
            and resolved_rel.startswith("localisation/")
            and not has_bom
        ):
            violations.append({"file": resolved_rel, "expected": "bom", "actual": "no-bom"})

    violation_page, truncated, total = paginate(violations, offset=offset, limit=limit)
    return enforce_budget(
        {
            "ok": True,
            "checked": len(targets),
            "total": total,
            "returned": len(violation_page),
            "truncated": truncated,
            "violations": violation_page,
            "counts": {
                "files_checked": len(targets),
                "violations": total,
            },
        },
        heavy_keys=("violations",),
    )
