"""Indexes for shared game content and country/unit history."""

from __future__ import annotations

from .base import FILE_NOT_FOUND, GenericTxtIndex
from .definitions import (
    parse_country_history_file,
    parse_dynamic_modifier_file,
    parse_modifier_definition_file,
    parse_on_action_file,
    parse_technology_file,
    parse_unit_history_file,
)


class OnActionIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "on_action"
    subdir = "common/on_actions"
    parser_fn = staticmethod(parse_on_action_file)
    primary_key = "id"
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True


class TechnologyIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "technology"
    subdir = "common/technologies"
    parser_fn = staticmethod(parse_technology_file)
    primary_key = "id"
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True


class DynamicModifierIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "dynamic_modifier"
    subdir = "common/dynamic_modifiers"
    parser_fn = staticmethod(parse_dynamic_modifier_file)
    primary_key = "id"
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True


class ModifierDefinitionIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "modifier_definition"
    subdir = "common/modifier_definitions"
    parser_fn = staticmethod(parse_modifier_definition_file)
    primary_key = "id"
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True


class CountryHistoryIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "country_history"
    subdir = "history/countries"
    parser_fn = staticmethod(parse_country_history_file)
    primary_key = "tag"
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True


class UnitHistoryIndex(GenericTxtIndex):
    cache_version = 1
    cache_name = "unit_history"
    subdir = "history/units"
    parser_fn = staticmethod(parse_unit_history_file)
    primary_key = "id"
    missing_result = FILE_NOT_FOUND
    track_parse_errors = True


# Both query types read the same parsed files and persistent cache. These aliases
# preserve descriptive public names without building the 851-file history twice.
DivisionTemplateIndex = UnitHistoryIndex
OobIndex = UnitHistoryIndex
