"""Country-tag universe index."""

from __future__ import annotations

from .base import GenericTxtIndex
from .definitions import parse_country_tag_file


class CountryTagIndex(GenericTxtIndex):
    """Index country tags and their country-history paths."""

    cache_version = 1
    cache_name = "country_tag"
    subdir = "common/country_tags"
    parser_fn = staticmethod(parse_country_tag_file)
    primary_key = "tag"
