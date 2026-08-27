"""Idea index — idea_id ↔ file/category/slot lookup."""

from __future__ import annotations

import logging
from typing import Optional

from ..paradox import parse_string
from ..paradox.schema import extract_idea_records
from ..util.encoding import read_text
from .base import GenericTxtIndex

logger = logging.getLogger(__name__)


def _parse_idea_file(abs_path: str, relpath: str) -> Optional[list[dict]]:
    try:
        text = read_text(abs_path)
    except OSError as e:
        logger.warning("idea index: cannot read %s: %s", abs_path, e)
        return None
    if "ideas" not in text:
        return []
    try:
        root = parse_string(text, error_prefix=f"In file {relpath}:\n")
    except Exception as e:
        logger.warning("idea index: parse failed for %s: %s", relpath, e)
        return None
    return extract_idea_records(root, source=text)


class IdeaIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "idea"
    subdir = "common/ideas"
    parser_fn = staticmethod(_parse_idea_file)
