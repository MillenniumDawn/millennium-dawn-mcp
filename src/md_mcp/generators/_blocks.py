"""Text helpers shared by the script scaffolders."""

from __future__ import annotations


def indent(body: str, tabs: int) -> list[str]:
    """Indent each non-blank line of `body` by `tabs` tabs; blank lines are left as they are."""
    pad = "\t" * tabs
    return [pad + line if line.strip() else line for line in body.rstrip("\n").splitlines()]


def block(name: str, body: str, tabs: int) -> list[str]:
    """Return `name = { body }` with the braces at `tabs` tabs and the body one deeper."""
    pad = "\t" * tabs
    return [f"{pad}{name} = {{", *indent(body, tabs + 1), f"{pad}}}"]
