"""Scripted-effect definition index."""

from __future__ import annotations

from .base import FILE_NOT_FOUND, GenericTxtIndex
from .definitions import parse_scripted_effect_file


class ScriptedEffectIndex(GenericTxtIndex):
    """Index top-level scripted effect blocks."""

    cache_version = 2
    cache_name = "scripted_effect"
    subdir = "common/scripted_effects"
    parser_fn = staticmethod(parse_scripted_effect_file)
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True
    tag_indexed = True
    source_tag_indexed = True
