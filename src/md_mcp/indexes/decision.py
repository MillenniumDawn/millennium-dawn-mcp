"""Decision index — decision_id ↔ file/category lookup."""

from __future__ import annotations

import logging
from typing import Optional

from ..paradox import parse_string
from ..paradox.schema import extract_decision_records
from ..util.encoding import read_text
from .base import GenericTxtIndex

logger = logging.getLogger(__name__)


def _parse_decision_file(abs_path: str, relpath: str) -> Optional[list[dict]]:
    try:
        text = read_text(abs_path)
    except OSError as e:
        logger.warning("decision index: cannot read %s: %s", abs_path, e)
        return None
    try:
        root = parse_string(text, error_prefix=f"In file {relpath}:\n")
    except Exception as e:
        logger.warning("decision index: parse failed for %s: %s", relpath, e)
        return None
    return extract_decision_records(root, source=text)


class DecisionIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "decision"
    subdir = "common/decisions"
    parser_fn = staticmethod(_parse_decision_file)
