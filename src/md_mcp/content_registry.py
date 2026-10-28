"""Declarative inventory of content handled by the analysis tools.

Keep canonical names, index constructors and file classification together so
new indexed content has one discoverable registration point.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContentType:
    name: str
    index_name: str | None = None
    path_prefixes: tuple[str, ...] = ()
    index_attribute: str | None = None
    manifest_categories: tuple[str, ...] = ()
    diff_extractor: str | None = None
    encoding_extensions: tuple[str, ...] = ()


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
