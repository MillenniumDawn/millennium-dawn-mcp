"""Encoding compliance check.

Per `Millennium-Dawn/.claude/rules/general-rules.md`:
  * `.txt` files must be UTF-8 **without** BOM (`EF BB BF`)
  * `.yml` files in `localisation/` must be UTF-8 **with** BOM

Walks the configured scan paths and reports every violation with the actual vs.
expected state. Returns `{ok, violations: [...], counts}`.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

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
    files: Optional[List[str]] = None,
) -> dict:
    """Verify BOM rules across the mod (or a specific file list).

    Args:
      files — explicit mod-relative paths to check; if omitted, walks the standard dirs

    Returns: `{ok, violations, counts}` where violations is
        [{file, expected: "no-bom" | "bom", actual: "no-bom" | "bom"}, ...]
    """
    targets: List[Path] = []
    if files:
        for f in files:
            p = (mod_root / f) if not Path(f).is_absolute() else Path(f)
            if p.exists():
                targets.append(p)
    else:
        for sub in _TXT_DIRS:
            d = mod_root / sub
            if d.is_dir():
                targets.extend(p for p in d.rglob("*.txt") if p.is_file())
        for sub in _YML_DIRS:
            d = mod_root / sub
            if d.is_dir():
                targets.extend(p for p in d.rglob("*.yml") if p.is_file())

    violations: List[dict] = []
    for path in targets:
        try:
            with open(path, "rb") as fh:
                head = fh.read(3)
        except OSError:
            continue

        has_bom = head == UTF8_BOM
        try:
            rel = str(path.relative_to(mod_root))
        except ValueError:
            rel = str(path)

        if path.suffix.lower() == ".txt" and has_bom:
            violations.append(
                {"file": rel, "expected": "no-bom", "actual": "bom"}
            )
        elif path.suffix.lower() == ".yml" and rel.startswith("localisation/") and not has_bom:
            violations.append(
                {"file": rel, "expected": "bom", "actual": "no-bom"}
            )

    return {
        "ok": True,
        "checked": len(targets),
        "violations": violations,
        "counts": {
            "files_checked": len(targets),
            "violations": len(violations),
        },
    }
