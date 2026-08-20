"""Parser tools — direct AST access for the agent.

Two tools: `parse_file` (read a path) and `parse_string` (parse a snippet). Output is
a tagged-union JSON structure (`{kind: scalar|block|symbol, ...}`) so the agent can
inspect or modify it without needing to know paradox-script syntax.

`parse_file` guards against blowing the output cap on massive files (vanilla event
files run to multiple MB). Past `max_bytes`, it refuses and tells the agent to use
a `resolve_*` tool instead, which returns just the target block.

`parse_file` only reads regular `.txt`/`.gfx` files under `mod_root` or `vanilla_path`;
see `_resolve_path` for the containment check.
"""

from __future__ import annotations

from pathlib import Path

from ..paradox import parse_string as _parse_string_impl
from ..paradox.schema import to_json_with_lines
from ..util.encoding import read_text
from ..util.line_numbers import line_starts, pos_to_line
from ..util.response import enforce_budget

_DEFAULT_MAX_BYTES = 500_000
_ALLOWED_EXTENSIONS = frozenset({".txt", ".gfx"})


class _PathAccessError(Exception):
    """`path` falls outside the allowed content roots, or isn't a parseable file."""


def parse_file_tool(
    path: str,
    mod_root: Path,
    vanilla_path: Path | None = None,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    top_level_only: bool = False,
) -> dict:
    """Parse a `.txt`/`.gfx` file under `mod_root` or `vanilla_path`.

    `path` may be absolute or relative to `mod_root`; relative paths always resolve
    against `mod_root`, never `vanilla_path`. Rejects paths that resolve outside both
    roots (including `..` traversal and symlink escapes), non-regular files, and
    unsupported extensions.

    Args:
      max_bytes       — refuse files larger than this (returns ok=False with hint).
                        Set to 0 to disable the guard.
      top_level_only  — return only the kind/name of each top-level node (no children),
                        a compact map for orienting in a large file.
    """
    try:
        resolved = _resolve_path(path, mod_root, vanilla_path)
    except _PathAccessError as e:
        return {"ok": False, "error": str(e)}

    try:
        size = resolved.stat().st_size
    except OSError as e:
        return {"ok": False, "error": f"Could not stat {resolved}: {e}"}

    if max_bytes and size > max_bytes:
        return {
            "ok": False,
            "error": (
                f"File is {size} bytes (> max_bytes={max_bytes}). Use resolve_focus/"
                "resolve_event/resolve_decision/resolve_idea for a single block, or "
                "raise max_bytes if you really need the whole AST."
            ),
            "path": str(resolved),
            "size": size,
        }

    text = read_text(resolved)
    try:
        root = _parse_string_impl(text, error_prefix=f"In file {resolved}:\n")
    except Exception as e:
        return {"ok": False, "error": str(e), "path": str(resolved)}

    if top_level_only:
        # Best-effort skim: top-level nodes' names + lines. Avoids serialising children.
        starts = line_starts(text)
        top: list[dict] = []
        for child in root.children():
            start = child.name_token.start if child.name_token else 0
            top.append(
                {
                    "name": child.name,
                    "line": pos_to_line(start, starts),
                }
            )
        return enforce_budget(
            {"ok": True, "path": str(resolved), "size": size, "top_level": top},
            heavy_keys=("top_level",),
        )

    return enforce_budget(
        {
            "ok": True,
            "path": str(resolved),
            "size": size,
            "root": to_json_with_lines(root, text),
        },
        heavy_keys=("root",),
    )


def parse_string_tool(text: str) -> dict:
    """Parse a snippet of paradox script. Useful for validating generator output."""
    try:
        root = _parse_string_impl(text)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return enforce_budget(
        {"ok": True, "root": to_json_with_lines(root, text)},
        heavy_keys=("root",),
    )


def _resolve_path(path: str, mod_root: Path, vanilla_path: Path | None) -> Path:
    """Resolve `path` to a regular `.txt`/`.gfx` file under `mod_root` or `vanilla_path`.

    Resolves symlinks and `..` before checking containment, so a symlink or traversal
    that escapes both roots is caught by the same check. Raises `_PathAccessError` for
    anything outside the roots, a non-regular file, or an unsupported extension.
    """
    p = Path(path)
    candidate = p if p.is_absolute() else mod_root / p
    resolved = candidate.resolve()

    roots = [mod_root.resolve()]
    if vanilla_path is not None:
        roots.append(vanilla_path.resolve())

    if not any(resolved.is_relative_to(root) for root in roots):
        allowed = " or ".join(str(root) for root in roots)
        raise _PathAccessError(f"{path!r} is outside the allowed content roots ({allowed})")

    if not resolved.is_file():
        raise _PathAccessError(f"{resolved} is not a regular file")

    if resolved.suffix not in _ALLOWED_EXTENSIONS:
        allowed_ext = ", ".join(sorted(_ALLOWED_EXTENSIONS))
        raise _PathAccessError(f"{resolved} has an unsupported extension (allowed: {allowed_ext})")

    return resolved
