"""Headless index build/load probe, with an import-safe entry point for spawn."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path
from unittest.mock import patch

from md_mcp.indexes import FocusIndex, LocalisationIndex
from md_mcp.indexes import base as base_mod
from md_mcp.indexes.base import GenericTxtIndex


def make_index(kind: str, root: Path, cache: Path) -> GenericTxtIndex:
    """Construct a mod-only focus or English localisation index."""
    if kind == "focus":
        return FocusIndex(root, cache, include_vanilla=False)
    return LocalisationIndex(root, cache, include_vanilla=False, langs=("en",))


def snapshot(index: GenericTxtIndex) -> dict:
    """JSON-compatible state, including duplicate winners, errors, and tag maps."""
    index.ensure_fresh()
    return json.loads(
        json.dumps(
            {
                "files": list(index._by_file.items()),
                "records": sorted(index._by_key.items(), key=lambda item: str(item[0])),
                "duplicates": sorted(index._duplicates.items(), key=lambda item: str(item[0])),
                "errors": index.parse_errors(),
                "ids_by_tag": index._ids_by_tag,
                "files_by_tag": index._files_by_tag,
            }
        )
    )


def main() -> None:
    """Build once and report exactly which source files and cache files were touched."""
    kind, root, cache, mode = sys.argv[1:]
    index = make_index(kind, Path(root), Path(cache))
    parsed: list[str] = []
    writes: list[str] = []
    contexts: list[str] = []
    parse = index._parse_parallel
    write = base_mod._atomic_write_text

    def counted_parse(relpaths):
        parsed.extend(relpaths)
        return parse(relpaths)

    def counted_write(path, text):
        writes.append(str(path.relative_to(cache)))
        return write(path, text)

    def spawn_context():
        contexts.append("spawn")
        return multiprocessing.get_context("spawn")

    if mode == "spawn":
        os.environ.pop("MD_MCP_SERIAL_PARSE", None)
    else:
        os.environ["MD_MCP_SERIAL_PARSE"] = "1"
    with (
        patch.object(index, "_parse_parallel", counted_parse),
        patch.object(base_mod, "_atomic_write_text", counted_write),
        patch.object(base_mod, "_default_workers", return_value=2),
        patch.object(
            base_mod,
            "_safe_process_context",
            spawn_context if mode == "spawn" else base_mod._safe_process_context,
        ),
    ):
        state = snapshot(index)
    print(
        json.dumps(
            {
                "state": state,
                "parsed": parsed,
                "writes": writes,
                "contexts": contexts,
                "pid": os.getpid(),
                "stdin": sys.stdin.read(),
            }
        )
    )


if __name__ == "__main__":
    main()
