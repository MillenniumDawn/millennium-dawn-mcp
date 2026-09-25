"""Shared parsers for lightweight script-definition indexes."""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..paradox import parse_string
from ..paradox.nodes import Node, SymbolNode
from ..util.encoding import read_text
from ..util.line_numbers import line_starts, pos_to_line

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,7}$")


def _scalar(node: Node) -> Optional[str]:
    value = node.value
    if isinstance(value, SymbolNode):
        return value.name
    if isinstance(value, str):
        return value
    return None


def _record(node: Node, relpath: str, starts: list[int], *, kind: str, **extra: str) -> dict:
    record = {
        "id": node.name,
        "kind": kind,
        "file": relpath,
        "line": pos_to_line(node.name_token.start, starts) if node.name_token else None,
    }
    record.update(extra)
    return record


def _parse_root(abs_path: str, relpath: str) -> tuple[Optional[Node], Optional[str]]:
    try:
        text = read_text(abs_path)
    except OSError as exc:
        logger.warning("definition index: cannot read %s", abs_path, exc)
        return None, None
    try:
        return parse_string(text, error_prefix=f"In file {relpath}:\n"), text
    except Exception as exc:
        logger.warning("definition index: parse failed for %s: %s", relpath, exc)
        return None, None


def parse_country_tag_file(abs_path: str, relpath: str) -> Optional[list[dict]]:
    root, text = _parse_root(abs_path, relpath)
    if root is None or text is None:
        return None
    starts = line_starts(text)
    records: list[dict] = []
    for node in root.children():
        tag = node.name
        country_file = _scalar(node)
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
    root, text = _parse_root(abs_path, relpath)
    if root is None or text is None:
        return None
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
    root, text = _parse_root(abs_path, relpath)
    if root is None or text is None:
        return None
    return _wrapped_records(root, relpath, line_starts(text), wrapper="leader_traits", kind="trait")


def _parse_scripted_file(abs_path: str, relpath: str, *, kind: str) -> Optional[list[dict]]:
    root, text = _parse_root(abs_path, relpath)
    if root is None or text is None:
        return None
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
    return records


def parse_scripted_effect_file(abs_path: str, relpath: str) -> Optional[list[dict]]:
    return _parse_scripted_file(abs_path, relpath, kind="scripted_effect")


def parse_scripted_trigger_file(abs_path: str, relpath: str) -> Optional[list[dict]]:
    return _parse_scripted_file(abs_path, relpath, kind="scripted_trigger")
