"""Shared parsers for lightweight script-definition indexes."""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..paradox import parse_string
from ..paradox.nodes import Node, node_line, symbol_or_str
from ..util.encoding import read_text
from ..util.line_numbers import line_starts
from .base import ParseResult

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,7}$")


def _record(node: Node, relpath: str, starts: list[int], *, kind: str, **extra: str) -> dict:
    record = {
        "id": node.name,
        "kind": kind,
        "file": relpath,
        "line": node_line(node, starts),
    }
    record.update(extra)
    return record


def _parse_root(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> tuple[Node, str] | ParseResult:
    """The parsed `(root, text)`, or the failed result for an unreadable or unparsable file."""
    try:
        # abs_path is constrained to the configured content roots.
        # pi-lens-ignore: python-path-traversal
        text = read_text(abs_path)
    except OSError as exc:
        logger.warning("definition index: cannot read %s: %s", abs_path, exc)
        return ParseResult.failed("read", exc)
    try:
        return (
            parse_string(
                text,
                error_prefix=f"In file {relpath}:\n",
                close_blocks_at_eof=close_blocks_at_eof,
            ),
            text,
        )
    except Exception as exc:
        logger.warning("definition index: parse failed for %s: %s", relpath, exc)
        return ParseResult.failed("parse", exc)


def parse_country_tag_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> Optional[list[dict]]:
    parsed = _parse_root(abs_path, relpath, close_blocks_at_eof)
    if isinstance(parsed, ParseResult):
        return None
    root, text = parsed
    starts = line_starts(text)
    records: list[dict] = []
    for node in root.children():
        tag = node.name
        country_file = symbol_or_str(node)
        if tag is None or not _TAG_RE.fullmatch(tag) or not country_file:
            continue
        records.append(
            _record(node, relpath, starts, kind="country_tag", tag=tag, country_file=country_file)
        )
    return records


def _wrapped_records(
    root: Node,
    relpath: str,
    starts: list[int],
    *,
    wrapper: str,
    kind: str,
) -> list[dict]:
    records: list[dict] = []
    for top in root.children():
        if top.name == wrapper and isinstance(top.value, list):
            for node in top.children():
                if node.name and isinstance(node.value, list):
                    records.append(_record(node, relpath, starts, kind=kind))
        elif (
            wrapper == "leader_traits"
            and top.name
            and isinstance(top.value, list)
            and relpath.lower().endswith("_traits.txt")
        ):
            # Some vanilla-style trait files omit the leader_traits container.
            # Files such as leader_skills use a different wrapper and are therefore
            # not mistaken for traits by the TraitIndex parser.
            records.append(_record(top, relpath, starts, kind=kind))
    return records


def parse_character_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> Optional[list[dict]]:
    parsed = _parse_root(abs_path, relpath, close_blocks_at_eof)
    if isinstance(parsed, ParseResult):
        return None
    root, text = parsed
    starts = line_starts(text)
    records: list[dict] = []
    for top in root.children():
        if top.name == "characters" and isinstance(top.value, list):
            records.extend(
                _record(node, relpath, starts, kind="character")
                for node in top.children()
                if node.name and isinstance(node.value, list)
            )
    return records


def parse_trait_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> Optional[list[dict]]:
    parsed = _parse_root(abs_path, relpath, close_blocks_at_eof)
    if isinstance(parsed, ParseResult):
        return None
    root, text = parsed
    return _wrapped_records(root, relpath, line_starts(text), wrapper="leader_traits", kind="trait")


def _parse_scripted_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool, *, kind: str
) -> ParseResult:
    parsed = _parse_root(abs_path, relpath, close_blocks_at_eof)
    if isinstance(parsed, ParseResult):
        return parsed
    root, text = parsed
    starts = line_starts(text)
    records: list[dict] = []
    for node in root.children():
        if node.name in {"scripted_effects", "scripted_triggers"} and isinstance(node.value, list):
            records.extend(
                _record(child, relpath, starts, kind=kind)
                for child in node.children()
                if child.name and isinstance(child.value, list)
            )
        elif node.name and isinstance(node.value, list):
            records.append(_record(node, relpath, starts, kind=kind))
    return ParseResult(records)


def parse_scripted_effect_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> ParseResult:
    return _parse_scripted_file(abs_path, relpath, close_blocks_at_eof, kind="scripted_effect")


def parse_scripted_trigger_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> ParseResult:
    return _parse_scripted_file(abs_path, relpath, close_blocks_at_eof, kind="scripted_trigger")


def parse_named_blocks_file(
    abs_path: str,
    relpath: str,
    close_blocks_at_eof: bool = False,
    *,
    kind: str,
    wrapper: str | None = None,
) -> ParseResult:
    """Index named top-level blocks, optionally inside one named container."""
    parsed = _parse_root(abs_path, relpath, close_blocks_at_eof)
    if isinstance(parsed, ParseResult):
        return parsed
    root, text = parsed
    starts = line_starts(text)
    records: list[dict] = []
    parents = [node for node in root.children() if node.name == wrapper] if wrapper else []
    candidates = (
        [child for parent in parents for child in parent.children()] if wrapper else root.children()
    )
    records.extend(
        _record(node, relpath, starts, kind=kind)
        for node in candidates
        if node.name and isinstance(node.value, list)
    )
    return ParseResult(records)


def parse_on_action_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> ParseResult:
    return parse_named_blocks_file(
        abs_path, relpath, close_blocks_at_eof, kind="on_action", wrapper="on_actions"
    )


def parse_technology_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> ParseResult:
    return parse_named_blocks_file(
        abs_path, relpath, close_blocks_at_eof, kind="technology", wrapper="technologies"
    )


def parse_dynamic_modifier_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> ParseResult:
    return parse_named_blocks_file(abs_path, relpath, close_blocks_at_eof, kind="dynamic_modifier")


def parse_modifier_definition_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> ParseResult:
    return parse_named_blocks_file(
        abs_path, relpath, close_blocks_at_eof, kind="modifier_definition"
    )


def parse_country_history_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> ParseResult:
    parsed = _parse_root(abs_path, relpath, close_blocks_at_eof)
    if isinstance(parsed, ParseResult):
        return parsed
    root, text = parsed
    stem = relpath.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    tag = stem.split(" - ", 1)[0].upper()
    return ParseResult(
        [
            {
                "id": tag,
                "tag": tag,
                "kind": "country_history",
                "file": relpath,
                "line": node_line(root.children()[0], line_starts(text)) if root.children() else 1,
            }
        ]
    )


def parse_unit_history_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> ParseResult:
    parsed = _parse_root(abs_path, relpath, close_blocks_at_eof)
    if isinstance(parsed, ParseResult):
        return parsed
    root, text = parsed
    starts = line_starts(text)
    records: list[dict] = []
    stem = relpath.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    for node in root.children():
        if node.name == "division_template" and isinstance(node.value, list):
            name = next(
                (symbol_or_str(child) for child in node.children() if child.name == "name"), None
            )
            if name:
                # The qualified key avoids collisions between countries/OOBs.
                records.append(
                    _record(
                        node,
                        relpath,
                        starts,
                        kind="division_template",
                        id=f"{stem}:{name}",
                        name=name,
                    )
                )
    anchor = root.children()[0] if root.children() else None
    records.append(
        _record(anchor, relpath, starts, kind="oob", id=stem)
        if anchor is not None
        else {"id": stem, "kind": "oob", "file": relpath, "line": 1}
    )
    return ParseResult(records)
