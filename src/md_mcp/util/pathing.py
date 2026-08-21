"""Mod-root discovery.

Resolution order (matches plan):
  1. Explicit `mod_root` (CLI flag → caller)
  2. `MD_MOD_ROOT` environment variable
  3. Walk up from `cwd` for a directory containing **both** `descriptor.mod` and `tools/validation/`
  4. Look for `Millennium-Dawn/descriptor.mod` in `cwd` and `cwd`'s parent
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Collection


class ModRootNotFound(RuntimeError):
    pass


class PathAccessError(ValueError):
    """A user-supplied path escapes the allowed content roots or fails a file check."""


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
        p = contained(root, relpath)
        if p is not None and p.exists():
            return p
    return None


def contained(root: Path, path: str | Path) -> Path | None:
    """`root / path` (absolute `path` passes through), or None if it escapes `root`.

    Resolves symlinks and `..` before the containment check, so symlink and
    traversal escapes are caught by the same test.
    """
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        candidate.relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return None
    return candidate


def validate_user_path(
    path: str | Path,
    roots: Path | list[Path],
    *,
    extensions: Collection[str] | None = None,
    require_file: bool = False,
) -> Path:
    """Resolve a user-supplied path against content roots, raising on escape.

    Relative paths resolve against the first root; absolute paths are used as-is.
    The resolved location must land inside at least one of `roots`. With
    `require_file`, the target must be a regular file; with `extensions`, its
    suffix must be in the allowlist.

    Raises `PathAccessError` when any check fails.
    """
    root_list = [roots] if isinstance(roots, Path) else list(roots)
    first = next(r for r in root_list if r is not None)
    p = Path(path)
    candidate = p if p.is_absolute() else first / p
    resolved = candidate.resolve()

    if not any(resolved.is_relative_to(root.resolve()) for root in root_list):
        allowed = " or ".join(str(root) for root in root_list)
        raise PathAccessError(f"{path!r} is outside the allowed content roots ({allowed})")

    if require_file and not resolved.is_file():
        raise PathAccessError(f"{resolved} is not a regular file")

    if extensions is not None and resolved.suffix not in extensions:
        allowed_ext = ", ".join(sorted(extensions))
        raise PathAccessError(f"{resolved} has an unsupported extension (allowed: {allowed_ext})")

    return resolved
