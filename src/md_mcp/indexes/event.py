"""Event index — `namespace.id` ↔ file lookup."""

from __future__ import annotations

import logging

from ..paradox import parse_string
from ..paradox.schema import EVENT_KINDS, extract_event_records
from ..util.encoding import read_text
from .base import FILE_NOT_FOUND, GenericTxtIndex, ParseResult

logger = logging.getLogger(__name__)


def _parse_event_file(abs_path: str, relpath: str) -> ParseResult:
    """Top-level so ProcessPoolExecutor can pickle and dispatch this."""
    try:
        text = read_text(abs_path)
    except OSError as e:
        logger.warning("event index: cannot read %s: %s", abs_path, e)
        return ParseResult.failed("read", e)
    if not any(tok in text for tok in EVENT_KINDS):
        return ParseResult([])
    try:
        root = parse_string(text, error_prefix=f"In file {relpath}:\n")
        return ParseResult(extract_event_records(root, source=text))
    except Exception as e:
        logger.warning("event index: parse failed for %s: %s", relpath, e)
        return ParseResult.failed("parse", e)


class EventIndex(GenericTxtIndex):
    cache_version = 2
    cache_name = "event"
    subdir = "events"
    parser_fn = staticmethod(_parse_event_file)
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True
