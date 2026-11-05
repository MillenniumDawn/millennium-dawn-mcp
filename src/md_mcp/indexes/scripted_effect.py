"""Scripted-effect definition index."""

from __future__ import annotations

from .base import FILE_NOT_FOUND, ParsedTxtIndex
from .definitions import parse_scripted_effect_file


class ScriptedEffectIndex(ParsedTxtIndex):
    """Index top-level scripted effect blocks."""

    # v3: parser acceptance widened (+ numbers, acute symbols, anonymous blocks,
    # vanilla EOF recovery); cached parse errors rebuild.
    cache_version = 4
    cache_name = "scripted_effect"
    subdir = "common/scripted_effects"
    parser_fn = staticmethod(parse_scripted_effect_file)
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True
    tag_indexed = True
    source_tag_indexed = True
