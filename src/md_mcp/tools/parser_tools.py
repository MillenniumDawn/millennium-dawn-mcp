"""Parser tools — direct AST access for the agent.

Two tools: `parse_file` (read a path) and `parse_string` (parse a snippet). Output is
a tagged-union JSON structure (`{kind: scalar|block|symbol, ...}`) so the agent can
inspect or modify it without needing to know paradox-script syntax.

`parse_file` guards against blowing the output cap on massive files (vanilla event
files run to multiple MB). Past `max_bytes`, it refuses and tells the agent to use
a `resolve_*` tool instead, which returns just the target block.
"""

from __future__ import annotations

from pathlib import Path

from ..paradox import parse_string as _parse_string_impl
from ..paradox.schema import to_json_with_lines
from ..util.encoding import read_text
from ..util.response import enforce_budget

_DEFAULT_MAX_BYTES = 500_000


def parse_file_tool(
    path: str,
    mod_root: Path,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    top_level_only: bool = False,
) -> dict:
    """Parse a `.txt` file. `path` may be absolute or relative to mod_root.

    Args:
      max_bytes       — refuse files larger than this (returns ok=False with hint).
                        Set to 0 to disable the guard.
      top_level_only  — return only the kind/name of each top-level node (no children),
                        a compact map for orienting in a large file.
    """
    resolved = _resolve_path(path, mod_root)
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
        line_starts = _line_starts(text)
        top: list[dict] = []
        for child in root.children():
            start = child.name_token.start if child.name_token else 0
            top.append(
                {
                    "name": child.name,
                    "line": _pos_to_line(start, line_starts),
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


def _resolve_path(path: str, mod_root: Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (mod_root / p).resolve()


def _line_starts(text: str) -> list[int]:
    """Cumulative offset of each line start. Used to translate `Token.start` → line number."""
    out = [0]
    running = 0
    for line in text.split("\n"):
        running += len(line) + 1
        out.append(running)
    return out


def _pos_to_line(pos: int, line_starts: list[int]) -> int:
    # Linear scan — adequate for top-level node counts (rarely > a few hundred).
    for i in range(len(line_starts) - 1):
        if pos < line_starts[i + 1]:
            return i + 1
    return len(line_starts)
