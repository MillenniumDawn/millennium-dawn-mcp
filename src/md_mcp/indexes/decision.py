"""Decision index — decision_id ↔ file/category lookup."""

from __future__ import annotations

import logging

from ..paradox import parse_string
from ..paradox.schema import extract_decision_records
from ..util.encoding import read_text
from .base import FILE_NOT_FOUND, GenericTxtIndex, ParseResult

logger = logging.getLogger(__name__)


def _parse_decision_file(
    abs_path: str, relpath: str, close_blocks_at_eof: bool = False
) -> ParseResult:
    """Vanilla-root files get parser EOF recovery; mod files stay strict."""
    try:
        text = read_text(abs_path)
    except OSError as e:
        logger.warning("decision index: cannot read %s: %s", abs_path, e)
        return ParseResult.failed("read", e)
    try:
        root = parse_string(
            text, error_prefix=f"In file {relpath}:\n", close_blocks_at_eof=close_blocks_at_eof
        )
        return ParseResult(extract_decision_records(root, source=text))
    except Exception as e:
        logger.warning("decision index: parse failed for %s: %s", relpath, e)
        return ParseResult.failed("parse", e)


class DecisionIndex(GenericTxtIndex):
    # v3: parser acceptance widened (+ numbers, acute symbols, anonymous blocks,
    # vanilla EOF recovery); cached parse errors rebuild.
    cache_version = 3
    cache_name = "decision"
    subdir = "common/decisions"
    parser_fn = staticmethod(_parse_decision_file)
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True
    tag_indexed = True
