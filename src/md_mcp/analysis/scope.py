"""Scope-file iteration — resolve, read, and parse a caller-supplied file list.

Every scoped analysis (layout, ref audit, deep focus filters) walks mod-relative
paths that may not exist or may not parse, and each has to keep going and report
the failure per file rather than abort the batch. This is that loop, once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set

from ..paradox import parse_string
from ..paradox.nodes import Node
from ..util.encoding import read_text
from ..util.pathing import resolve_scope_file

_MAX_ERROR_CHARS = 200


@dataclass
class ScopeFile:
    """One successfully resolved and parsed scope file."""

    relpath: str
    root: Node
    text: str


def iter_scope_files(
    relpaths: Iterable[str],
    mod_root: Path,
    vanilla_path: Optional[Path],
    errors: List[dict],
    failed_files: Optional[Set[str]] = None,
) -> Iterator[ScopeFile]:
    """Yield parsed scope files, recording mod-then-vanilla resolution errors in order."""
    for relpath in relpaths:
        abs_path = resolve_scope_file(relpath, mod_root, vanilla_path)
        if abs_path is None:
            _record_error(errors, failed_files, relpath, "not found")
            continue
        try:
            # abs_path is constrained to mod_root/vanilla by resolve_scope_file.
            # pi-lens-ignore: python-path-traversal
            text = read_text(abs_path)
            root = parse_string(text)
        except Exception as exc:
            _record_error(errors, failed_files, relpath, str(exc)[:_MAX_ERROR_CHARS])
            continue
        yield ScopeFile(relpath, root, text)


def _record_error(
    errors: List[dict], failed_files: Optional[Set[str]], relpath: str, message: str
) -> None:
    errors.append({"file": relpath, "error": message})
    if failed_files is not None:
        failed_files.add(relpath)
