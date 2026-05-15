"""Focus block scaffolder.

Output follows the Millennium Dawn focus tree reference:
  * Tab-indented
  * Property order: id, icon, x, y, relative_position_id, cost, prerequisite,
    mutually_exclusive, search_filters, available, completion_reward, ai_will_do
  * Always emits `log = ...` inside `completion_reward`
  * Returns plain text content — no BOM (per general-rules.md)

Returns `{txt: str, loc_yml_keys: [{key, value}]}`. The agent uses Edit/Write to
place the text into the right file; loc keys are designed to be appended to
`MD_focus_<TAG>_l_english.yml`.
"""

from __future__ import annotations

from typing import List, Optional


def generate_focus(
    *,
    id: str,
    tag: str,
    x: int,
    y: int,
    cost: float = 10,
    icon: Optional[str] = None,
    relative_position_id: Optional[str] = None,
    prerequisites: Optional[List[List[str]]] = None,
    mutually_exclusive: Optional[List[str]] = None,
    search_filters: Optional[List[str]] = None,
    available: Optional[str] = None,
    completion_reward: Optional[str] = None,
    ai_base: int = 1,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Scaffold a `focus = { ... }` block + matching loc keys.

    Args:
      id                    — focus ID (e.g. `ISR_idf_modernization`); TAG must be uppercase
      tag                   — country tag (used for the auto search filter + log target)
      x, y                  — grid coordinates
      cost                  — political-power cost in 70-day chunks (default 10 = 1 month)
      icon                  — GFX_ sprite name; omit and a placeholder is inserted
      relative_position_id  — anchor focus id (recommended for non-root focuses)
      prerequisites         — list of prereq groups; each group is OR'd internally,
                              groups themselves are AND'd. e.g. [["A"], ["B", "C"]]
                              means `A AND (B OR C)`
      mutually_exclusive    — list of mutex focus ids
      search_filters        — full filter list; omit and a default of
                              `FOCUS_FILTER_<TAG>` (country) + a generic one is suggested
      available             — raw paradox-script content for the `available` block
                              (no surrounding braces)
      completion_reward     — raw paradox-script content for the reward block
                              (no surrounding braces, log is auto-prepended)
      ai_base               — `ai_will_do.base` weight (1 = default)
      title, description    — loc values; default to placeholder strings
    """
    parts: List[str] = ["\tfocus = {"]
    parts.append(f"\t\tid = {id}")
    parts.append(f"\t\ticon = GFX_{icon or 'placeholder_focus'}")
    parts.append("")
    parts.append(f"\t\tx = {x}")
    parts.append(f"\t\ty = {y}")
    if relative_position_id:
        parts.append(f"\t\trelative_position_id = {relative_position_id}")
    parts.append("")
    parts.append(f"\t\tcost = {cost:g}")
    parts.append("")

    if prerequisites:
        for group in prerequisites:
            joined = " ".join(f"focus = {p}" for p in group)
            parts.append(f"\t\tprerequisite = {{ {joined} }}")
    if mutually_exclusive:
        joined = " ".join(f"focus = {m}" for m in mutually_exclusive)
        parts.append(f"\t\tmutually_exclusive = {{ {joined} }}")
    if prerequisites or mutually_exclusive:
        parts.append("")

    filters = search_filters or [f"FOCUS_FILTER_{tag.upper()}POLIT", "FOCUS_FILTER_POLITICAL"]
    parts.append(f"\t\tsearch_filters = {{ {' '.join(filters)} }}")
    parts.append("")

    if available:
        parts.append("\t\tavailable = {")
        for line in _indent_lines(available, 3):
            parts.append(line)
        parts.append("\t\t}")
        parts.append("")

    parts.append("\t\tcompletion_reward = {")
    parts.append(f'\t\t\tlog = "[GetDateText]: [Root.GetName]: Focus {id}"')
    if completion_reward:
        for line in _indent_lines(completion_reward, 3):
            parts.append(line)
    else:
        parts.append("\t\t\t# add_political_power = 50")
    parts.append("\t\t}")
    parts.append("")

    parts.append("\t\tai_will_do = {")
    parts.append(f"\t\t\tbase = {ai_base}")
    parts.append("\t\t}")
    parts.append("\t}")

    txt = "\n".join(parts)

    title_default = title or _humanise(id, tag)
    desc_default = description or "TODO: focus description."
    loc_yml_keys = [
        {"key": id, "value": title_default},
        {"key": f"{id}_desc", "value": desc_default},
    ]

    return {"txt": txt, "loc_yml_keys": loc_yml_keys}


def _indent_lines(block: str, tabs: int) -> List[str]:
    pad = "\t" * tabs
    return [pad + line if line.strip() else line for line in block.rstrip("\n").splitlines()]


def _humanise(focus_id: str, tag: str) -> str:
    """Strip the tag prefix and Title-Case the rest as a placeholder title."""
    prefix = tag.upper() + "_"
    body = focus_id[len(prefix) :] if focus_id.upper().startswith(prefix) else focus_id
    return body.replace("_", " ").title()
