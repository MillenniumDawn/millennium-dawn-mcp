"""Mod-root discovery.

Resolution order (matches plan):
  1. Explicit `mod_root` (CLI flag → caller)
  2. `MD_MOD_ROOT` environment variable
  3. Walk up from `cwd` for a directory containing **both** `descriptor.mod` and `tools/validation/`
  4. Look for `Millennium-Dawn/descriptor.mod` in `cwd` and `cwd`'s parent
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath


class ModRootNotFound(RuntimeError):
    pass


def find_mod_root(explicit: str | Path | None = None, start: Path | None = None) -> Path:
    """Locate the Millennium-Dawn mod root, raising ModRootNotFound with the attempts on failure."""
    attempts: list[str] = []

    if explicit is not None:
        p = Path(explicit).expanduser().resolve()
        if _looks_like_mod_root(p):
            return p
        attempts.append(f"explicit --mod-root: {p}")

    env = os.environ.get("MD_MOD_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if _looks_like_mod_root(p):
            return p
        attempts.append(f"MD_MOD_ROOT env var: {p}")

    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        if _looks_like_mod_root(parent):
            return parent
    attempts.append(
        f"walk-up from cwd ({cwd}): no parent had both descriptor.mod and tools/validation/"
    )

    for base in (cwd, cwd.parent):
        candidate = base / "Millennium-Dawn"
        if _looks_like_mod_root(candidate):
            return candidate
    attempts.append(f"./Millennium-Dawn and ../Millennium-Dawn relative to {cwd}: not found")

    msg = "Could not locate Millennium-Dawn mod root. Tried:\n  - " + "\n  - ".join(attempts)
    msg += "\n\nSet MD_MOD_ROOT, pass --mod-root, or run from inside the mod tree."
    raise ModRootNotFound(msg)


def _looks_like_mod_root(p: Path) -> bool:
    return p.is_dir() and (p / "descriptor.mod").exists() and (p / "tools" / "validation").is_dir()


def resolve_scope_file(relpath: str, mod_root: Path, vanilla_path: Path | None) -> Path | None:
    """Locate a scope file, falling back to vanilla for files the mod doesn't override.

    `relpath` is caller-supplied (a tool argument), so it must stay inside the
    root it resolves against: absolute paths and `..` traversal are rejected
    rather than read.
    """
    for root in (mod_root, vanilla_path):
        if root is None:
            continue
        p = _contained(root, relpath)
        if p is not None and p.exists():
            return p
    return None


def _contained(root: Path, relpath: str) -> Path | None:
    """`root / relpath`, or None if it escapes `root`."""
    if PurePath(relpath).is_absolute():
        return None
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def find_vanilla_path(mod_root: Path) -> Path | None:
    """Auto-detect a sibling `Hearts of Iron IV/` directory. Returns None if absent.

    Honours the HOI4_PATH env var as override.
    """
    env = os.environ.get("HOI4_PATH")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None

    sibling = mod_root.parent / "Hearts of Iron IV"
    return sibling if sibling.is_dir() else None
