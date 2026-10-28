"""Declarative inventory of content handled by the analysis tools.

Keep canonical names, index constructors and file classification together so
new indexed content has one discoverable registration point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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


CONTENT_TYPES: tuple[ContentType, ...] = (
    ContentType(
        "focus", "FocusIndex", ("common/national_focus/",), "focus_index", ("focuses",), "focus"
    ),
    ContentType(
        "event", "EventIndex", ("events/",), "event_index", ("events", "event_files"), "event"
    ),
    ContentType(
        "decision",
        "DecisionIndex",
        ("common/decisions/",),
        "decision_index",
        ("decisions",),
        "decision",
    ),
    ContentType("idea", "IdeaIndex", ("common/ideas/",), "idea_index", ("ideas",), "idea"),
    ContentType(
        "loc",
        "LocalisationIndex",
        ("localisation/",),
        "loc_index",
        ("loc_files",),
        encoding_extensions=(".yml",),
    ),
    ContentType("gfx", "GfxIndex", ("interface/",), "gfx_index"),
    ContentType(
        "country_tag",
        "CountryTagIndex",
        index_attribute="country_tag_index",
        manifest_categories=("country_tags",),
    ),
    ContentType(
        "character",
        "CharacterIndex",
        index_attribute="character_index",
        manifest_categories=("characters", "character_files"),
    ),
    ContentType(
        "trait",
        "TraitIndex",
        index_attribute="trait_index",
        manifest_categories=("traits", "trait_files"),
    ),
    ContentType(
        "scripted_effect",
        "ScriptedEffectIndex",
        index_attribute="scripted_effect_index",
        manifest_categories=("scripted_effects", "scripted_effect_files"),
    ),
    ContentType(
        "scripted_trigger",
        "ScriptedTriggerIndex",
        index_attribute="scripted_trigger_index",
        manifest_categories=("scripted_triggers", "scripted_trigger_files"),
    ),
    ContentType(
        "mio",
        path_prefixes=("common/military_industrial_organization/",),
        manifest_categories=("mio_files",),
    ),
    ContentType(
        "history", path_prefixes=("history/",), manifest_categories=("history_files", "oob_files")
    ),
    ContentType("tools", path_prefixes=("tools/",)),
    ContentType("claude", path_prefixes=(".claude/",)),
    # Text-only reference kinds share the same registry even though they have
    # no definition index.
    ContentType("sprite"),
    ContentType("flag"),
    ContentType("variable"),
)

CONTENT_BY_NAME = {item.name: item for item in CONTENT_TYPES}
INDEX_TYPES = tuple(item for item in CONTENT_TYPES if item.index_name)
INDEX_TYPE_BY_NAME = {item.index_name: item for item in INDEX_TYPES}

REFERENCE_KIND_ALIASES = {
    "tag": "country_tag",
    "scripted_effects": "scripted_effect",
    "scripted_triggers": "scripted_trigger",
}

ENCODING_TEXT_DIRS = ("common", "events", "history", "interface")
ENCODING_YML_DIRS = ("localisation",)
MANIFEST_CATEGORIES = (
    "focuses",
    "events",
    "event_files",
    "decisions",
    "ideas",
    "loc_files",
    "mio_files",
    "history_files",
    "oob_files",
    "namelist_files",
    "country_tags",
    "characters",
    "character_files",
    "traits",
    "trait_files",
    "scripted_effects",
    "scripted_effect_files",
    "scripted_triggers",
    "scripted_trigger_files",
)


def classify_content_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    for item in CONTENT_TYPES:
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


REFERENCE_PATTERNS = {
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
REFERENCE_SCAN_DIRS = {
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

REFERENCE_EXTENSIONS = {
    # Loc keys appear in both the defining .yml files and as bare identifiers in
    # script (.txt) files used as titles / descriptions / tooltip targets.
    "loc": (".yml", ".txt"),
    "sprite": (".txt", ".gui", ".gfx"),
}

AUDIT_NODE_SETS = {
    "event": frozenset({"country_event", "news_event"}),
    "focus": frozenset({"has_completed_focus", "complete_national_focus", "relative_position_id"}),
    "idea_block": frozenset({"add_ideas", "remove_ideas"}),
    "idea_symbol": frozenset({"add_idea", "remove_idea", "idea", "has_idea"}),
    "sprite": frozenset({"icon", "picture"}),
    "loc": frozenset({"custom_effect_tooltip"}),
    "decision": frozenset({"activate_decision", "unlock_decision_tooltip"}),
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

AUDIT_RESOLVER_INDEX_ATTRS = {
    "focus": "focus_index",
    "event": "event_index",
    "idea": "idea_index",
    "loc": "loc_index",
    "decision": "decision_index",
    "country_tag": "country_tag_index",
    "character": "character_index",
    "trait": "trait_index",
    "scripted_effect": "scripted_effect_index",
    "scripted_trigger": "scripted_trigger_index",
}
AUDIT_SYMBOL_REFERENCE_KINDS = ("focus", "idea", "loc", "decision")
AUDIT_NOT_CHECKED = ("country_flags", "variables", "scripted_effects", "scripted_triggers")
AUDIT_DEFAULT_KINDS = (
    "focus",
    "event",
    "idea",
    "sprite",
    "loc",
    "decision",
    "country_tag",
    "character",
    "trait",
    "scripted_effect",
    "scripted_trigger",
)
