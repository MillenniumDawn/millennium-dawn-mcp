"""Idea scaffolder.

Generates a single idea block intended to be placed inside an existing
`ideas = { <category> = { ... } }` structure. Categories like `country` accept
ideas directly; categories like `tank_manufacturer` accept ideas inside a slot
wrapper (e.g. `designer = yes`) — the caller picks the placement.
"""

from __future__ import annotations

from typing import List, Optional


def generate_idea(
    *,
    id: str,
    tag: Optional[str] = None,
    picture: Optional[str] = None,
    modifier: Optional[str] = None,
    research_bonus: Optional[str] = None,
    equipment_bonus: Optional[str] = None,
    targeted_modifier: Optional[str] = None,
    allowed: Optional[str] = None,
    available: Optional[str] = None,
    cancel: Optional[str] = None,
    cost: Optional[int] = None,
    removal_cost: int = -1,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Scaffold a single idea definition.

    Args:
      id                   — idea id (typically `TAG_<thing>_idea`)
      tag                  — country tag for the auto-allowed `original_tag = TAG`
      picture              — GFX picture; placeholder if omitted
      modifier             — raw script for `modifier = { ... }`
      research_bonus       — raw script for `research_bonus = { ... }`
      equipment_bonus      — raw script for `equipment_bonus = { ... }`
      targeted_modifier    — raw script for `targeted_modifier = { ... }`
      allowed              — raw script for `allowed = { ... }`; auto-builds
                             `original_tag = TAG` if omitted and `tag` is supplied
      available            — raw script for `available = { ... }`
      cancel               — raw script for `cancel = { ... }` (rarely useful)
      cost                 — political-power cost (only relevant for some categories)
      removal_cost         — `removal_cost = N` (default -1 = unremovable)
      title, description   — loc values for `ID` / `ID_desc`
    """
    parts: List[str] = [f"\t\t{id} = {{"]
    parts.append(f"\t\t\tpicture = {picture or 'generic_idea'}")

    if allowed or tag:
        parts.append("\t\t\tallowed = {")
        if allowed:
            parts.extend(_indent(allowed, 4))
        else:
            parts.append(f"\t\t\t\toriginal_tag = {tag}")
        parts.append("\t\t\t}")

    if available:
        parts.append("\t\t\tavailable = {")
        parts.extend(_indent(available, 4))
        parts.append("\t\t\t}")

    if cancel:
        parts.append("\t\t\tcancel = {")
        parts.extend(_indent(cancel, 4))
        parts.append("\t\t\t}")

    if cost is not None:
        parts.append(f"\t\t\tcost = {cost}")
    if removal_cost != -1:
        parts.append(f"\t\t\tremoval_cost = {removal_cost}")

    if modifier:
        parts.append("\t\t\tmodifier = {")
        parts.extend(_indent(modifier, 4))
        parts.append("\t\t\t}")

    if research_bonus:
        parts.append("\t\t\tresearch_bonus = {")
        parts.extend(_indent(research_bonus, 4))
        parts.append("\t\t\t}")

    if equipment_bonus:
        parts.append("\t\t\tequipment_bonus = {")
        parts.extend(_indent(equipment_bonus, 4))
        parts.append("\t\t\t}")

    if targeted_modifier:
        parts.append("\t\t\ttargeted_modifier = {")
        parts.extend(_indent(targeted_modifier, 4))
        parts.append("\t\t\t}")

    parts.append("\t\t}")

    return {
        "txt": "\n".join(parts),
        "loc_yml_keys": [
            {"key": id, "value": title or id.replace("_", " ").title()},
            {"key": f"{id}_desc", "value": description or "TODO: idea description."},
        ],
    }


def _indent(block: str, tabs: int) -> List[str]:
    pad = "\t" * tabs
    return [pad + line if line.strip() else line for line in block.rstrip("\n").splitlines()]
