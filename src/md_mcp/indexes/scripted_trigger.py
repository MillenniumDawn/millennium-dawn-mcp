"""Scripted-trigger definition index."""

from __future__ import annotations

from .base import GenericTxtIndex
from .definitions import parse_scripted_trigger_file


class ScriptedTriggerIndex(GenericTxtIndex):
    """Index top-level scripted trigger blocks."""

    cache_version = 1
    cache_name = "scripted_trigger"
    subdir = "common/scripted_triggers"
    parser_fn = staticmethod(parse_scripted_trigger_file)
    primary_key = "id"
