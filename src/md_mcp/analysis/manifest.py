"""Per-country file manifest.

`list_country_content(tag)` returns every focus / event / decision / idea / MIO / loc
file owned by a tag. Useful as a starting point when the agent is told "work on USA"
and wants the file shortlist instead of grepping.

Country-owned files follow two conventions in Millennium Dawn (cf. CLAUDE.md and
`.claude/rules/`):

  * Focus, decision, idea, MIO, history, OOB files often start with `TAG_` or `<int>_TAG_`
  * Localisation files are `MD_focus_TAG_l_english.yml` (one file per country)
  * Events go in `events/<CountryName>.txt` — the filename uses the long name, not the
    tag, so we cross-reference via the event index records that start with `<TAG>.` (e.g.
    `Afghanistan.3` for tag AFG).

Output-size aware. By default returns only counts and a small sample of each
category; pass `include=[...]` to opt in to full lists for specific categories.
USA in the real mod has 700+ focuses + 1000+ loc rows — returning all of them by
default exceeds MCP output caps.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence

from ..indexes import (
    CharacterIndex,
    CountryTagIndex,
    DecisionIndex,
    EventIndex,
    FocusIndex,
    IdeaIndex,
    LocalisationIndex,
    ScriptedEffectIndex,
    ScriptedTriggerIndex,
    TraitIndex,
)
from ..indexes.localisation import has_lang_suffix
from ..util.response import enforce_budget

_ALL_CATEGORIES = (
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


def list_country_content(
    tag: str,
    mod_root: Path,
    *,
    submod_root: Optional[Path] = None,
    focus_index: Optional[FocusIndex] = None,
    event_index: Optional[EventIndex] = None,
    decision_index: Optional[DecisionIndex] = None,
    idea_index: Optional[IdeaIndex] = None,
    loc_index: Optional[LocalisationIndex] = None,
    country_tag_index: Optional[CountryTagIndex] = None,
    character_index: Optional[CharacterIndex] = None,
    trait_index: Optional[TraitIndex] = None,
    scripted_effect_index: Optional[ScriptedEffectIndex] = None,
    scripted_trigger_index: Optional[ScriptedTriggerIndex] = None,
    include: Optional[Sequence[str]] = None,
    limit_per_category: int = 100,
) -> dict:
    """Manifest of files / IDs owned by a tag.

    Args:
      include            — which categories to return in full. Anything not listed
                           returns only its count (+ a tiny sample). Pass `["*"]` for
                           all categories. Default = counts-only for everything.
      limit_per_category — when a category is included, cap its list at this many entries.
    """
    tag_upper = tag.upper()
    prefix = tag_upper + "_"
    wanted_full = _resolve_include(include)

    focuses: list[str] = _ids_for_tag(focus_index, tag_upper)
    decisions: list[str] = _ids_for_tag(decision_index, tag_upper)
    ideas: list[str] = _ids_for_tag(idea_index, tag_upper)
    events, event_files = _events(event_index, tag_upper, prefix)
    loc_files: list[str] = _loc_files(loc_index, tag_upper)
    mio_files = _scan_files(
        mod_root,
        "common/military_industrial_organization/organizations",
        prefix=tag_upper,
        submod_root=submod_root,
    )
    history_files = _scan_files(
        mod_root, "history/countries", prefix=tag_upper, submod_root=submod_root
    )
    oob_files = _scan_files(mod_root, "history/units", prefix=tag_upper, submod_root=submod_root)
    namelist_files = _scan_files(
        mod_root, "common/names", prefix=tag_upper, submod_root=submod_root
    )
    country_tags = _tag_records(country_tag_index, tag_upper)
    characters, character_files = _indexed_country_records(character_index, tag_upper)
    traits, trait_files = _indexed_country_records(trait_index, tag_upper)
    scripted_effects, scripted_effect_files = _indexed_country_records(
        scripted_effect_index, tag_upper
    )
    scripted_triggers, scripted_trigger_files = _indexed_country_records(
        scripted_trigger_index, tag_upper
    )

    raw: dict[str, list[str]] = {
        "focuses": sorted(focuses),
        "events": sorted(events),
        "event_files": sorted(event_files),
        "decisions": sorted(decisions),
        "ideas": sorted(ideas),
        "loc_files": sorted(loc_files),
        "mio_files": mio_files,
        "history_files": history_files,
        "oob_files": oob_files,
        "namelist_files": namelist_files,
        "country_tags": country_tags,
        "characters": characters,
        "character_files": character_files,
        "traits": traits,
        "trait_files": trait_files,
        "scripted_effects": scripted_effects,
        "scripted_effect_files": scripted_effect_files,
        "scripted_triggers": scripted_triggers,
        "scripted_trigger_files": scripted_trigger_files,
    }

    counts = {cat: len(items) for cat, items in raw.items()}
    result: dict = {"ok": True, "tag": tag_upper, "counts": counts}

    for cat, items in raw.items():
        if cat in wanted_full:
            truncated = len(items) > limit_per_category
            result[cat] = items[:limit_per_category]
            if truncated:
                result[f"{cat}_truncated"] = True
        else:
            # Small sample so the agent knows what shape the IDs take.
            sample = items[:5]
            if sample:
                result[f"{cat}_sample"] = sample

    if not wanted_full:
        result["hint"] = (
            "Returning counts + samples only. Pass include=['focuses','events',...] "
            "or include=['*'] for full lists, with limit_per_category to cap each."
        )

    return enforce_budget(result, heavy_keys=tuple(_ALL_CATEGORIES))


def _resolve_include(include: Optional[Sequence[str]]) -> set[str]:
    if not include:
        return set()
    if "*" in include or "all" in include:
        return set(_ALL_CATEGORIES)
    return {c for c in include if c in _ALL_CATEGORIES}


def _ids_for_tag(index, tag_upper: str) -> list[str]:
    return index.ids_for_tag(tag_upper) if index is not None else []


def _events(
    event_index: Optional[EventIndex], tag_upper: str, prefix: str
) -> tuple[list[str], list[str]]:
    if event_index is None:
        return [], []
    event_index.ensure_fresh()
    events: list[str] = []
    files: list[str] = []
    seen_files: set[str] = set()
    for eid in event_index.list_keys():
        rec = event_index.resolve(eid)
        if rec is None:
            continue
        file = rec["file"]
        stem = Path(file).stem
        if stem.upper() == tag_upper or stem.upper().startswith(prefix):
            events.append(eid)
            if file not in seen_files:
                files.append(file)
                seen_files.add(file)
    return events, files


def _tag_file_pattern(tag_upper: str) -> re.Pattern[str]:
    """Match `TAG` as a path or name segment; `\\` is a separator too (Windows relpaths)."""
    return re.compile(rf"(^|[\\/_]){re.escape(tag_upper)}(_|[\\/]|$)")


def _loc_files(loc_index: Optional[LocalisationIndex], tag_upper: str) -> list[str]:
    """Every `*_l_<lang>.yml` named for the tag, in every language, over the index's roots.

    Walks the filesystem rather than the index's records: the index holds only
    the configured `loc_langs`, and a country manifest must list every
    translation. The roots (submod, mod, vanilla) come from the index so the
    listing matches what the index would see.
    """
    if loc_index is None:
        return []
    pattern = _tag_file_pattern(tag_upper)
    out: set[str] = set()
    for root in loc_index._roots():
        d = root / "localisation"
        if not d.is_dir():
            continue
        for p in d.rglob("*.yml"):
            if not p.is_file() or not has_lang_suffix(p):
                continue
            rel = str(p.relative_to(root))
            if rel not in out and pattern.search(rel):
                out.add(rel)
    return sorted(out)


def _tag_records(index: Optional[CountryTagIndex], tag_upper: str) -> list[str]:
    if index is None:
        return []
    rec = index.resolve(tag_upper)
    return [rec["id"]] if rec is not None else []


def _indexed_country_records(index, tag_upper: str) -> tuple[list[str], list[str]]:
    if index is None:
        return [], []
    ids: list[str] = []
    files: set[str] = set()
    for key in index.ids_for_country_tag(tag_upper):
        rec = index.resolve(key)
        if rec is None:
            continue
        ids.append(str(key))
        files.add(str(rec["file"]))
    return sorted(set(ids)), sorted(files)


def _scan_files(
    mod_root: Path,
    subdir: str,
    *,
    prefix: str,
    submod_root: Optional[Path] = None,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    roots = [root for root in (submod_root, mod_root) if root is not None]
    for root in roots:
        d = root / subdir
        if not d.is_dir():
            continue
        for p in d.rglob("*.txt"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            if rel in seen:
                continue
            stem = p.stem.upper()
            matched = stem.startswith(prefix + "_") or stem == prefix
            if not matched and "_" in stem:
                matched = stem.split("_")[1] == prefix
            if matched:
                seen.add(rel)
                out.append(rel)
    return sorted(out)
