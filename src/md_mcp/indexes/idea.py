"""Idea index — idea_id ↔ file/category/slot lookup."""

from __future__ import annotations

import logging

from ..paradox import parse_string
from ..paradox.schema import extract_idea_records
from ..util.encoding import read_text
from .base import FILE_NOT_FOUND, GenericTxtIndex, ParseResult

logger = logging.getLogger(__name__)


def _parse_idea_file(abs_path: str, relpath: str) -> ParseResult:
    try:
        text = read_text(abs_path)
    except OSError as e:
        logger.warning("idea index: cannot read %s: %s", abs_path, e)
        return ParseResult.failed("read", e)
    if "ideas" not in text:
        return ParseResult([])
    try:
        root = parse_string(text, error_prefix=f"In file {relpath}:\n")
        return ParseResult(extract_idea_records(root, source=text))
    except Exception as e:
        logger.warning("idea index: parse failed for %s: %s", relpath, e)
        return ParseResult.failed("parse", e)


class IdeaIndex(GenericTxtIndex):
    cache_version = 2
    cache_name = "idea"
    subdir = "common/ideas"
    parser_fn = staticmethod(_parse_idea_file)
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True
    tag_indexed = True
