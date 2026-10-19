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


def _parse_root(abs_path: str, relpath: str) -> tuple[Node, str] | ParseResult:
    """The parsed `(root, text)`, or the failed result for an unreadable or unparsable file."""
    try:
        text = read_text(abs_path)
    except OSError as exc:
        logger.warning("definition index: cannot read %s: %s", abs_path, exc)
        return ParseResult.failed("read", exc)
    try:
        return parse_string(text, error_prefix=f"In file {relpath}:\n"), text
    except Exception as exc:
        logger.warning("definition index: parse failed for %s: %s", relpath, exc)
        return ParseResult.failed("parse", exc)


def parse_country_tag_file(abs_path: str, relpath: str) -> Optional[list[dict]]:
    parsed = _parse_root(abs_path, relpath)
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


def parse_character_file(abs_path: str, relpath: str) -> Optional[list[dict]]:
    parsed = _parse_root(abs_path, relpath)
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


def parse_trait_file(abs_path: str, relpath: str) -> Optional[list[dict]]:
    parsed = _parse_root(abs_path, relpath)
    if isinstance(parsed, ParseResult):
        return None
    root, text = parsed
    return _wrapped_records(root, relpath, line_starts(text), wrapper="leader_traits", kind="trait")


def _parse_scripted_file(abs_path: str, relpath: str, *, kind: str) -> ParseResult:
    parsed = _parse_root(abs_path, relpath)
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


def parse_scripted_effect_file(abs_path: str, relpath: str) -> ParseResult:
    return _parse_scripted_file(abs_path, relpath, kind="scripted_effect")


def parse_scripted_trigger_file(abs_path: str, relpath: str) -> ParseResult:
    return _parse_scripted_file(abs_path, relpath, kind="scripted_trigger")
