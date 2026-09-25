"""Scripted-effect definition index."""

from __future__ import annotations

from .base import GenericTxtIndex
from .definitions import parse_scripted_effect_file


class ScriptedEffectIndex(GenericTxtIndex):
    """Index top-level scripted effect blocks."""

    cache_version = 1
    cache_name = "scripted_effect"
    subdir = "common/scripted_effects"
    parser_fn = staticmethod(parse_scripted_effect_file)
    primary_key = "id"
