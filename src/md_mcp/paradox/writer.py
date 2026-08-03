"""Render a parsed AST back to paradox script.

Used by the schema layer's `to_json()` (for round-trip diagnostics) and by the M3
generators that compose AST fragments programmatically.

Format conventions enforced (per `Millennium-Dawn/.claude/rules/general-rules.md`):
    * Tabs for indentation
    * UTF-8, no BOM (caller's responsibility on write)
"""

from __future__ import annotations

from typing import Iterable

from .nodes import Node, SymbolNode


def node_to_str(node: Node, indent: int = 0) -> str:
    """Render a single Node (or the file root) to its paradox-script form."""
    if node.name is None and isinstance(node.value, list):
        # File root.
        return _render_children(node.value, indent)

    return _render_node(node, indent)


def _render_node(node: Node, indent: int) -> str:
    pad = "\t" * indent
    if node.operator is None:
        # Bare keyword.
        return f"{pad}{node.name}"

    op = node.operator
    val = _render_value(node.value, indent)

    if node.value_attachment is not None:
        # `name = attachment { ... }` form. The block went into `value`; attachment is the symbol.
        return f"{pad}{node.name} {op} {node.value_attachment.name} {val.lstrip()}"

    if isinstance(node.value, list):
        # Block — keep opening brace on same line.
        return f"{pad}{node.name} {op} {val.lstrip()}"

    return f"{pad}{node.name} {op} {val}"


def _render_value(value: object, indent: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, SymbolNode):
        return value.name
    if isinstance(value, (int, float)):
        return _format_number(value)
    if isinstance(value, str):
        return f'"{_escape_string(value)}"'
    if isinstance(value, list):
        if not value:
            return "{ }"
        inner = _render_children(value, indent + 1)
        pad = "\t" * indent
        return "{\n" + inner + "\n" + pad + "}"
    raise TypeError(f"Cannot render value of type {type(value).__name__}")


def _render_children(children: Iterable[Node], indent: int) -> str:
    return "\n".join(_render_node(c, indent) for c in children)


def _format_number(n: int | float) -> str:
    if isinstance(n, int):
        return str(n)
    # Keep trailing zero off — paradox accepts plain decimals.
    s = repr(n)
    return s


def _escape_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
