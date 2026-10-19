"""Scripted-trigger definition index."""

from __future__ import annotations

from .base import FILE_NOT_FOUND, GenericTxtIndex
from .definitions import parse_scripted_trigger_file


class ScriptedTriggerIndex(GenericTxtIndex):
    """Index top-level scripted trigger blocks."""

    cache_version = 2
    cache_name = "scripted_trigger"
    subdir = "common/scripted_triggers"
    parser_fn = staticmethod(parse_scripted_trigger_file)
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True
    tag_indexed = True
    source_tag_indexed = True
