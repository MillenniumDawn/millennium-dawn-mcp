"""Match hierarchy-valid candidates to indexed source lines."""

from __future__ import annotations

from typing import Optional, Sequence, TypeVar

T = TypeVar("T")


class IndexAnchorError(KeyError):
    """An index record cannot be matched to exactly one definition in the file."""


def select_indexed_anchor(
    entries: Sequence[tuple[Optional[int], T]],
    *,
    line: Optional[int],
    kind: str,
    ident: str,
) -> T:
    """Require a unique indexed-line match, or a lone candidate without a line."""
    if not entries:
        raise IndexAnchorError(
            f"{kind} '{ident}' is indexed but no matching definition remains in the file; "
            "the index is stale — delete <mod_root>/.md-mcp-cache/ and rerun "
            "`md-mcp build-index`",
        )

    if line is not None:
        on_line = [item for entry_line, item in entries if entry_line == line]
        if not on_line:
            raise IndexAnchorError(
                f"{kind} '{ident}' index points at line {line} but no matching definition "
                "starts there; the index is stale — delete <mod_root>/.md-mcp-cache/ and "
                "rerun `md-mcp build-index`",
            )
        if len(on_line) > 1:
            raise IndexAnchorError(
                f"{kind} '{ident}' is ambiguous: {len(on_line)} matching definitions start "
                f"on line {line} and the index cannot distinguish them",
            )
        return on_line[0]

    if len(entries) > 1:
        raise IndexAnchorError(
            f"{kind} '{ident}' is ambiguous: {len(entries)} matching definitions found and "
            "the index has no line to disambiguate",
        )
    return entries[0][1]
