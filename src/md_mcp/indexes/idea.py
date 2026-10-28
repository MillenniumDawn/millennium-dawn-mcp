"""Idea index — idea_id ↔ file/category/slot lookup."""

from __future__ import annotations

import logging

from ..paradox import parse_string
from ..paradox.schema import extract_idea_records
from ..util.encoding import read_text
from .base import FILE_NOT_FOUND, GenericTxtIndex, ParseResult

logger = logging.getLogger(__name__)


def _parse_idea_file(abs_path: str, relpath: str, close_blocks_at_eof: bool = False) -> ParseResult:
    """Vanilla-root files get parser EOF recovery; mod files stay strict."""
    try:
        text = read_text(abs_path)
    except OSError as e:
        logger.warning("idea index: cannot read %s: %s", abs_path, e)
        return ParseResult.failed("read", e)
    if "ideas" not in text:
        return ParseResult([])
    try:
        root = parse_string(
            text, error_prefix=f"In file {relpath}:\n", close_blocks_at_eof=close_blocks_at_eof
        )
        return ParseResult(extract_idea_records(root, source=text))
    except Exception as e:
        logger.warning("idea index: parse failed for %s: %s", relpath, e)
        return ParseResult.failed("parse", e)


class IdeaIndex(GenericTxtIndex):
    # v4: ideas holding only allowed_civil_war/on_add/on_remove now recognized; caches rebuild.
    cache_version = 4
    cache_name = "idea"
    subdir = "common/ideas"
    parser_fn = staticmethod(_parse_idea_file)
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True
    tag_indexed = True
