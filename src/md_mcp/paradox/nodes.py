"""AST node types for HOI4 paradox script.

Ported from `MD-VSCode-Utility-Tool/src/hoiformat/hoiparser.ts`.

Node.value is a tagged union — represented in Python as one of:
  * None              — keyword-only (e.g. `add_namespace`)
  * str               — string literal (quotes stripped, escapes resolved)
  * int | float       — numeric literal
  * SymbolNode        — bare identifier (e.g. `yes`, `TAG`, `idea_name`)
  * list[Node]        — block contents `{ ... }`
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from ..util.line_numbers import pos_to_line


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int
    type: str


@dataclass(frozen=True)
class SymbolNode:
    name: str


NodeValue = Union[None, str, int, float, SymbolNode, list]


@dataclass
class Node:
    """A parsed `name = value` pair or block element.

    For the file-root node, name/operator are None and value is the list of top-level nodes.
    For a bare keyword (e.g. inside `{ A B C }`), operator and value are None and name holds it.

    Only the tokens something reads are kept: the name (line numbers, slice start)
    and the value's last token (slice end). Every extra token is held per node.
    """

    name: Optional[str] = None
    operator: Optional[str] = None
    value: NodeValue = None
    value_attachment: Optional[SymbolNode] = None

    name_token: Optional[Token] = None
    value_end_token: Optional[Token] = None

    def children(self) -> list["Node"]:
        """Return the list of child nodes if value is a block, else []."""
        return self.value if isinstance(self.value, list) else []

    def get(self, name: str) -> Optional["Node"]:
        """Return the first child with the given name (case-insensitive), or None."""
        target = name.lower()
        for child in self.children():
            if child.name and child.name.lower() == target:
                return child
        return None

    def get_all(self, name: str) -> list["Node"]:
        """Return all children with the given name (case-insensitive)."""
        target = name.lower()
        return [c for c in self.children() if c.name and c.name.lower() == target]


def symbol_or_str(node: Optional[Node]) -> Optional[str]:
    """Return the text of a symbol or string value, or None for any other value."""
    if node is None:
        return None
    value = node.value
    if isinstance(value, SymbolNode):
        return value.name
    if isinstance(value, str):
        return value
    return None


def node_line(node: Node, starts: Optional[list[int]]) -> Optional[int]:
    """Return the 1-based line of `node`'s name, or None without a name token or line table."""
    if starts is None or node.name_token is None:
        return None
    return pos_to_line(node.name_token.start, starts)
