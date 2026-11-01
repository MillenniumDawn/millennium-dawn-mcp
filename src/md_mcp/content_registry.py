"""Declarative inventory of content handled by the analysis tools.

Keep canonical names, index constructors and file classification together so
new indexed content has one discoverable registration point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .indexes import (
    CharacterIndex,
    CountryTagIndex,
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
    ScriptedEffectIndex,
    ScriptedTriggerIndex,
    TraitIndex,
)
from .paradox.schema import EVENT_KINDS


@dataclass(frozen=True)
class ContentType:
    name: str
    index_name: str | None = None
    path_prefixes: tuple[str, ...] = ()
    index_attribute: str | None = None
    manifest_categories: tuple[str, ...] = ()
    diff_extractor: str | None = None
    encoding_extensions: tuple[str, ...] = ()
    audit_default: bool = False
    audit_default_order: int = 0
    audit_symbol_nodes: tuple[str, ...] = ()
    audit_resolver: bool = False
    encoding_dirs: tuple[str, ...] = ()
    index_class: Any = None
    reference_pattern: Any = None
    reference_dirs: tuple[str, ...] = ()
    reference_extensions: tuple[str, ...] = ()
    manifest_extractor: str | None = None
    not_checked_label: str | None = None
    not_checked_order: int = 0
    encoding_order: int = 0


CONTENT_TYPES: tuple[ContentType, ...] = (
    ContentType(
        "focus",
        "FocusIndex",
        ("common/national_focus/",),
        "focus_index",
        ("focuses",),
        "focus",
        index_class=FocusIndex,
        audit_default=True,
        audit_default_order=0,
        audit_symbol_nodes=(
            "has_completed_focus",
            "complete_national_focus",
            "relative_position_id",
        ),
        audit_resolver=True,
        encoding_dirs=("common",),
    ),
    ContentType(
        "event",
        "EventIndex",
        ("events/",),
        "event_index",
        ("events", "event_files"),
        "event",
        index_class=EventIndex,
        audit_default=True,
        audit_default_order=1,
        audit_resolver=True,
        encoding_dirs=("events",),
        encoding_order=1,
    ),
    ContentType(
        "decision",
        "DecisionIndex",
        ("common/decisions/",),
        "decision_index",
        ("decisions",),
        "decision",
        index_class=DecisionIndex,
        audit_default=True,
        audit_default_order=5,
        audit_symbol_nodes=("activate_decision", "unlock_decision_tooltip"),
        audit_resolver=True,
        encoding_dirs=("common",),
    ),
    ContentType(
        "idea",
        "IdeaIndex",
        ("common/ideas/",),
        "idea_index",
        ("ideas",),
        "idea",
        index_class=IdeaIndex,
        audit_default=True,
        audit_default_order=2,
        audit_symbol_nodes=("add_idea", "remove_idea", "idea", "has_idea"),
        audit_resolver=True,
        encoding_dirs=("common",),
    ),
    ContentType(
        "loc",
        "LocalisationIndex",
        ("localisation/",),
        "loc_index",
        ("loc_files",),
        index_class=LocalisationIndex,
        audit_default=True,
        audit_default_order=4,
        audit_symbol_nodes=("custom_effect_tooltip",),
        audit_resolver=True,
        encoding_extensions=(".yml",),
        encoding_dirs=("localisation",),
    ),
    ContentType(
        "gfx",
        "GfxIndex",
        ("interface/",),
        "gfx_index",
        index_class=GfxIndex,
        encoding_dirs=("interface",),
        encoding_order=3,
    ),
    ContentType(
        "country_tag",
        "CountryTagIndex",
        index_attribute="country_tag_index",
        manifest_categories=("country_tags",),
        index_class=CountryTagIndex,
        audit_default=True,
        audit_default_order=6,
        audit_resolver=True,
        encoding_dirs=("common",),
    ),
    ContentType(
        "character",
        "CharacterIndex",
        index_attribute="character_index",
        manifest_categories=("characters", "character_files"),
        index_class=CharacterIndex,
        audit_default=True,
        audit_default_order=7,
        audit_resolver=True,
        encoding_dirs=("common",),
    ),
    ContentType(
        "trait",
        "TraitIndex",
        index_attribute="trait_index",
        manifest_categories=("traits", "trait_files"),
        index_class=TraitIndex,
        audit_default=True,
        audit_default_order=8,
        audit_resolver=True,
        encoding_dirs=("common",),
    ),
    ContentType(
        "scripted_effect",
        "ScriptedEffectIndex",
        index_attribute="scripted_effect_index",
        manifest_categories=("scripted_effects", "scripted_effect_files"),
        index_class=ScriptedEffectIndex,
        audit_default=True,
        audit_default_order=9,
        audit_resolver=True,
        encoding_dirs=("common",),
        not_checked_label="scripted_effects",
        not_checked_order=2,
    ),
    ContentType(
        "scripted_trigger",
        "ScriptedTriggerIndex",
        index_attribute="scripted_trigger_index",
        manifest_categories=("scripted_triggers", "scripted_trigger_files"),
        index_class=ScriptedTriggerIndex,
        audit_default=True,
        audit_default_order=10,
        audit_resolver=True,
        encoding_dirs=("common",),
        not_checked_label="scripted_triggers",
        not_checked_order=3,
    ),
    ContentType(
        "mio",
        path_prefixes=("common/military_industrial_organization/",),
        manifest_categories=("mio_files",),
        encoding_dirs=("common",),
    ),
    ContentType(
        "history",
        path_prefixes=("history/",),
        manifest_categories=("history_files", "oob_files"),
        encoding_dirs=("history",),
        encoding_order=2,
    ),
    ContentType(
        "namelist",
        path_prefixes=("common/names/",),
        manifest_categories=("namelist_files",),
        encoding_dirs=("common",),
    ),
    ContentType("tools", path_prefixes=("tools/",)),
    ContentType("claude", path_prefixes=(".claude/",)),
    ContentType(
        "sprite",
        audit_default=True,
        audit_default_order=3,
        encoding_dirs=("interface",),
        encoding_order=3,
    ),
    ContentType("flag", not_checked_label="country_flags", not_checked_order=0),
    ContentType("variable", not_checked_label="variables", not_checked_order=1),
)

REFERENCE_KIND_ALIASES = {
    "tag": "country_tag",
    "scripted_effects": "scripted_effect",
    "scripted_triggers": "scripted_trigger",
}


def classify_content_path(path: str) -> str:
    return classify_registered_path(path, CONTENT_TYPES)


def classify_registered_path(path: str, content_types: tuple[ContentType, ...]) -> str:
    normalized = path.replace("\\", "/")
    for item in content_types:
        if any(normalized.startswith(prefix) for prefix in item.path_prefixes):
            if item.name == "gfx" and not normalized.endswith(".gfx"):
                continue
            return item.name
    return "other"


# For each kind, define:
#   * subdirs to scan (mod-relative)
#   * extensions to match (.txt usually; .yml for loc)
#   * a regex generator that, given the target id, returns the pattern to search for
def _identifier(target: str) -> str:
    return _target_group(re.escape(target))


def _target_group(body: str) -> str:
    # The only capture in each pattern: `_active_matches` judges this span.
    return r'(?<![\w.])("' + body + r'"|' + body + r")(?![\w.])"


def _focus_pattern(target: str) -> re.Pattern:
    # `focus = X`, `prerequisite = { focus = X }`, `mutually_exclusive = { focus = X }`,
    # `complete_national_focus = X`, `mark_focus_tree_layout_dirty` style false-positives skipped.
    return re.compile(
        r"\b(?:focus|complete_national_focus|unlock_national_focus)\s*=\s*" + _identifier(target)
    )


def _event_pattern(target: str) -> re.Pattern:
    # `country_event = X`, `country_event = { id = X ... }`, `news_event = X`, etc.
    event_kinds = "|".join(re.escape(kind) for kind in EVENT_KINDS)
    return re.compile(
        rf"\b(?:{event_kinds})\s*=\s*(?:"
        + _identifier(target)
        + r"|\{\s*(?:[^{}]*\s)?id\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _decision_pattern(target: str) -> re.Pattern:
    # `activate_decision = X`, `unlock_decision_tooltip = X`, decision target in tooltips.
    return re.compile(
        r"\b(?:activate_decision|unlock_decision_tooltip|remove_decision|decision)\s*=\s*"
        + _identifier(target)
    )


def _idea_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:has_idea|add_ideas|remove_ideas|swap_ideas|add_timed_idea)\s*=\s*"
        + r"(?:"
        + _identifier(target)
        + r"|\{\s*(?:[^{}]*\s)?(?:idea|add)\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _flag_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:has_country_flag|set_country_flag|clr_country_flag|modify_country_flag"
        r"|has_global_flag|set_global_flag|clr_global_flag|modify_global_flag"
        r"|has_state_flag|set_state_flag|clr_state_flag"
        r"|has_character_flag|set_character_flag|clr_character_flag)\s*=\s*"
        + r"(?:"
        + _identifier(target)
        + r"|\{\s*flag\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _variable_pattern(target: str) -> re.Pattern:
    # References can be explicit (`var = X`), scoped (`var = ROOT.X`),
    # or short-form assignments (`set_temp_variable = { X = 10 }`).
    target_ref = _identifier(target)
    if "." not in target:
        target_ref = _target_group(r"(?:[A-Za-z_]\w*\.)?" + re.escape(target))
    operators = (
        r"var|has_variable|set_variable|set_temp_variable|add_to_variable|subtract_from_variable"
        r"|multiply_variable|divide_variable|check_variable|clamp_variable|modulo_variable"
    )
    return re.compile(
        r"\b(?:"
        + operators
        + r")\s*=\s*(?:"
        + target_ref
        + r"|\{\s*(?:[^{}]*?\s)?(?:var|name)\s*=\s*"
        + target_ref
        + r"(?:\s|\})|\{\s*(?:[^{}]*?\s)?"
        + target_ref
        + r"\s*=)"
    )


def _loc_pattern(target: str) -> re.Pattern:
    # Both `KEY:` definitions and `[KEY]` / `$KEY$` references.
    return re.compile(_identifier(target))


def _sprite_pattern(target: str) -> re.Pattern:
    return re.compile(_identifier(target))


def _named_definition_pattern(target: str) -> re.Pattern:
    """Match a top-level definition or direct scripted call by name."""
    return re.compile(_identifier(target) + r"\s*=\s*(?:\{|[^\s{}#]+)")


def _country_tag_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:original_tag|tag|change_tag|set_cosmetic_tag|target_tag|"
        r"original_tag_to_check|tag_to_check)\s*=\s*(?:"
        + _identifier(target)
        + r"|\{\s*(?:tag|original_tag)\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _character_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:character|has_character|create_character|remove_character|"
        r"modify_character|set_character)\s*=\s*(?:"
        + _identifier(target)
        + r"|\{\s*character\s*=\s*"
        + _identifier(target)
        + r")"
    )


def _trait_pattern(target: str) -> re.Pattern:
    return re.compile(
        r"\b(?:trait|has_trait|add_trait|remove_trait|remove_leader_trait)\s*=\s*(?:"
        + _identifier(target)
        + r"|\{\s*trait\s*=\s*"
        + _identifier(target)
        + r")"
    )


_REFERENCE_PATTERN_BUILDERS = {
    "focus": _focus_pattern,
    "event": _event_pattern,
    "decision": _decision_pattern,
    "idea": _idea_pattern,
    "loc": _loc_pattern,
    "sprite": _sprite_pattern,
    "flag": _flag_pattern,
    "variable": _variable_pattern,
    "country_tag": _country_tag_pattern,
    "character": _character_pattern,
    "trait": _trait_pattern,
    "scripted_effect": _named_definition_pattern,
    "scripted_trigger": _named_definition_pattern,
}

KIND_ALIASES = REFERENCE_KIND_ALIASES


# Directories to scan per reference kind. Keep the set small to minimise false positives.
_REFERENCE_DIRS = {
    "focus": [
        "common/national_focus",
        "events",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/on_actions",
        "common/ideas",
    ],
    "event": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/on_actions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/ideas",
    ],
    "decision": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/on_actions",
        "common/scripted_effects",
        "common/scripted_triggers",
    ],
    "idea": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/ideas",
        "history/countries",
    ],
    "flag": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/ideas",
    ],
    "variable": [
        "events",
        "common/national_focus",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/ideas",
    ],
    # Loc keys are *defined* in localisation/, but *referenced* across all script
    # files (focus titles, event descs, tooltips, custom_effect_tooltip targets).
    "loc": [
        "localisation/english",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/ideas",
        "common/scripted_localisation",
        "common/scripted_effects",
        "common/scripted_triggers",
    ],
    "sprite": [
        "common/national_focus",
        "common/decisions",
        "common/ideas",
        "interface",
    ],
    "country_tag": [
        "common/country_tags",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/ideas",
        "common/scripted_effects",
        "common/scripted_triggers",
        "history/countries",
    ],
    "character": [
        "common/characters",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/scripted_effects",
        "common/scripted_triggers",
        "history/countries",
        "history/units",
    ],
    "trait": [
        "common/country_leader",
        "common/unit_leader",
        "common/characters",
        "common/national_focus",
        "events",
        "common/scripted_effects",
        "common/scripted_triggers",
        "history/units",
    ],
    "scripted_effect": [
        "common/scripted_effects",
        "common/scripted_triggers",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/ideas",
        "common/on_actions",
        "history/countries",
    ],
    "scripted_trigger": [
        "common/scripted_triggers",
        "common/scripted_effects",
        "common/national_focus",
        "events",
        "common/decisions",
        "common/ideas",
        "common/on_actions",
        "history/countries",
    ],
}

_REFERENCE_EXTENSION_OVERRIDES = {
    # Loc keys appear in both the defining .yml files and as bare identifiers in
    # script (.txt) files used as titles / descriptions / tooltip targets.
    "loc": (".yml", ".txt"),
    "sprite": (".txt", ".gui", ".gfx"),
}

AUDIT_NODE_SETS = {
    "event": frozenset({"country_event", "news_event"}),
    "idea_block": frozenset({"add_ideas", "remove_ideas"}),
    "sprite": frozenset({"icon", "picture"}),
    "country_tag": frozenset(
        {"original_tag", "tag", "change_tag", "target_tag", "original_tag_to_check", "tag_to_check"}
    ),
    "character": frozenset(
        {
            "character",
            "has_character",
            "create_character",
            "remove_character",
            "modify_character",
            "set_character",
        }
    ),
    "trait": frozenset({"trait", "has_trait", "add_trait", "remove_trait", "remove_leader_trait"}),
    "scope_keywords": frozenset({"ROOT", "FROM", "PREV", "THIS", "OWNER", "CONTROLLER"}),
    "dotted_scope_prefixes": ("var:", "event_target:"),
    "focus_definitions": frozenset({"focus", "shared_focus", "joint_focus"}),
}


def prepare_content_types(content_types: tuple[ContentType, ...]) -> tuple[ContentType, ...]:
    """Fill built-in reference defaults while preserving metadata on new entries."""
    return tuple(
        replace(
            item,
            reference_pattern=_REFERENCE_PATTERN_BUILDERS.get(item.name, item.reference_pattern),
            reference_dirs=tuple(_REFERENCE_DIRS.get(item.name, item.reference_dirs)),
            reference_extensions=_REFERENCE_EXTENSION_OVERRIDES.get(
                item.name, item.reference_extensions or (".txt",)
            ),
        )
        for item in content_types
    )


def build_registry_views(content_types: tuple[ContentType, ...]) -> dict[str, Any]:
    """Build consumer lookups from registry entries (also used by fixtures)."""

    def encoding_dirs_for(extension: str) -> tuple[str, ...]:
        directory_order: dict[str, int] = {}
        for item in content_types:
            if item.encoding_extensions and extension not in item.encoding_extensions:
                continue
            if not item.encoding_extensions and extension != ".txt":
                continue
            for directory in item.encoding_dirs:
                directory_order[directory] = min(
                    directory_order.get(directory, item.encoding_order), item.encoding_order
                )
        return tuple(sorted(directory_order, key=lambda directory: directory_order[directory]))

    return {
        "by_name": {item.name: item for item in content_types},
        "index_types": tuple(item for item in content_types if item.index_class is not None),
        "reference_patterns": {
            item.name: item.reference_pattern
            for item in content_types
            if item.reference_pattern is not None
        },
        "reference_scan_dirs": {
            item.name: item.reference_dirs
            for item in content_types
            if item.reference_pattern is not None
        },
        "reference_extensions": {
            item.name: item.reference_extensions
            for item in content_types
            if item.reference_pattern is not None and item.reference_extensions != (".txt",)
        },
        "manifest_categories": tuple(
            category for item in content_types for category in item.manifest_categories
        ),
        "not_checked": tuple(
            item.not_checked_label
            for item in sorted(content_types, key=lambda content: content.not_checked_order)
            if item.not_checked_label is not None
        ),
        "encoding_text_dirs": encoding_dirs_for(".txt"),
        "encoding_yml_dirs": encoding_dirs_for(".yml"),
        "audit_resolvers": {
            item.name: item.index_attribute
            for item in content_types
            if item.audit_resolver and item.index_attribute is not None
        },
        "audit_symbol_kinds": tuple(item.name for item in content_types if item.audit_symbol_nodes),
        "audit_default_kinds": tuple(
            item.name
            for item in sorted(content_types, key=lambda content: content.audit_default_order)
            if item.audit_default
        ),
    }


# Resolve the forward declarations above into the runtime registry once all
# builders and scan rules are available. Consumers use only this catalog.
CONTENT_TYPES = prepare_content_types(CONTENT_TYPES)
_VIEWS = build_registry_views(CONTENT_TYPES)
CONTENT_BY_NAME = _VIEWS["by_name"]
INDEX_TYPES = _VIEWS["index_types"]
INDEX_TYPE_BY_NAME = {item.index_name: item for item in INDEX_TYPES}
REFERENCE_PATTERNS = _VIEWS["reference_patterns"]
REFERENCE_SCAN_DIRS = _VIEWS["reference_scan_dirs"]
REFERENCE_EXTENSIONS = _VIEWS["reference_extensions"]
MANIFEST_CATEGORIES = _VIEWS["manifest_categories"]
ENCODING_TEXT_DIRS = _VIEWS["encoding_text_dirs"]
ENCODING_YML_DIRS = _VIEWS["encoding_yml_dirs"]
AUDIT_RESOLVER_INDEX_ATTRS = _VIEWS["audit_resolvers"]
AUDIT_SYMBOL_REFERENCE_KINDS = _VIEWS["audit_symbol_kinds"]
AUDIT_DEFAULT_KINDS = _VIEWS["audit_default_kinds"]
AUDIT_NOT_CHECKED = _VIEWS["not_checked"]
