"""Character and leader-trait indexes."""

from __future__ import annotations

from .base import GenericTxtIndex
from .definitions import parse_character_file, parse_trait_file


class CharacterIndex(GenericTxtIndex):
    """Index character definitions from ``common/characters``."""

    cache_version = 1
    cache_name = "character"
    subdir = "common/characters"
    parser_fn = staticmethod(parse_character_file)
    primary_key = "id"


class TraitIndex(GenericTxtIndex):
    """Index country and unit leader traits."""

    cache_version = 1
    cache_name = "trait"
    subdirs = ("common/country_leader", "common/unit_leader")
    parser_fn = staticmethod(parse_trait_file)
    primary_key = "id"
