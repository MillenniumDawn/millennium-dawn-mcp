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
    """

    name: Optional[str] = None
    operator: Optional[str] = None
    value: NodeValue = None
    value_attachment: Optional[SymbolNode] = None

    name_token: Optional[Token] = None
    operator_token: Optional[Token] = None
    value_attachment_token: Optional[Token] = None
    value_start_token: Optional[Token] = None
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
