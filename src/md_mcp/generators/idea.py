"""Idea scaffolder.

Generates a single idea block intended to be placed inside an existing
`ideas = { <category> = { ... } }` structure. Categories like `country` accept
ideas directly; categories like `tank_manufacturer` accept ideas inside a slot
wrapper (e.g. `designer = yes`) — the caller picks the placement.
"""

from __future__ import annotations

from typing import Optional

from ..util.response import enforce_budget
from ._blocks import block, indent


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
    parts: list[str] = [f"\t\t{id} = {{"]
    parts.append(f"\t\t\tpicture = {picture or 'generic_idea'}")

    if allowed or tag:
        parts.append("\t\t\tallowed = {")
        if allowed:
            parts.extend(indent(allowed, 4))
        else:
            parts.append(f"\t\t\t\toriginal_tag = {tag}")
        parts.append("\t\t\t}")

    if available:
        parts.extend(block("available", available, 3))

    if cancel:
        parts.extend(block("cancel", cancel, 3))

    if cost is not None:
        parts.append(f"\t\t\tcost = {cost}")
    if removal_cost != -1:
        parts.append(f"\t\t\tremoval_cost = {removal_cost}")

    if modifier:
        parts.extend(block("modifier", modifier, 3))

    if research_bonus:
        parts.extend(block("research_bonus", research_bonus, 3))

    if equipment_bonus:
        parts.extend(block("equipment_bonus", equipment_bonus, 3))

    if targeted_modifier:
        parts.extend(block("targeted_modifier", targeted_modifier, 3))

    parts.append("\t\t}")

    return enforce_budget(
        {
            "txt": "\n".join(parts),
            "loc_yml_keys": [
                {"key": id, "value": title or id.replace("_", " ").title()},
                {"key": f"{id}_desc", "value": description or "TODO: idea description."},
            ],
        },
        heavy_keys=("txt", "loc_yml_keys"),
    )
