"""Decision scaffolder.

Produces a `<decision_id> = { ... }` block that goes inside an existing decision
category (e.g. `<category> = { ... <decision_id> = { ... } ... }`). Returns the
block as a string plus loc keys.

The output is structured to satisfy `check_common_mistakes.py`:
  * Dynamic conditions go in `available`, not `allowed` (allowed locks at game start)
  * `original_tag` instead of `tag` when restricting to a country (civil-war safe)
"""

from __future__ import annotations

from typing import List, Optional


def generate_decision(
    *,
    id: str,
    tag: Optional[str] = None,
    icon: Optional[str] = None,
    cost: int = 25,
    days_remove: Optional[int] = None,
    days_re_enable: Optional[int] = None,
    allowed: Optional[str] = None,
    visible: Optional[str] = None,
    available: Optional[str] = None,
    complete_effect: Optional[str] = None,
    remove_effect: Optional[str] = None,
    cancel_trigger: Optional[str] = None,
    state_target: bool = False,
    target_root_trigger: Optional[str] = None,
    target_trigger: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Scaffold a single decision block.

    Args:
      id                  — decision id
      tag                 — country tag for the auto-allowed `original_tag = X` guard
      icon                — GFX_decision_<icon> reference; placeholder if omitted
      cost                — political-power cost (or other resource, depending on script)
      days_remove         — `days_remove = N` (decision stays active for N days)
      days_re_enable      — cooldown after completion
      allowed             — raw script for the once-at-load `allowed = { ... }` block
                            (typically only `original_tag = X` and dlc gates)
      visible             — raw script for `visible = { ... }`
      available           — raw script for `available = { ... }` (dynamic conditions)
      complete_effect     — raw script for `complete_effect = { ... }`
      remove_effect       — raw script for `remove_effect = { ... }` (when `days_remove`)
      cancel_trigger      — raw script for `cancel_trigger = { ... }`
      state_target        — set `state_target = yes` (state-scope decision)
      target_root_trigger — `target_root_trigger = { ... }` (cheap initial filter)
      target_trigger      — `target_trigger = { FROM = { ... } }` (per-target filter)
      title, description  — loc values for `ID` / `ID_desc`
    """
    parts: List[str] = [f"\t{id} = {{"]
    parts.append(f"\t\ticon = {icon or 'generic_decision'}")
    parts.append("")

    if state_target:
        parts.append("\t\tstate_target = yes")
    if target_root_trigger:
        parts.append("\t\ttarget_root_trigger = {")
        parts.extend(_indent(target_root_trigger, 3))
        parts.append("\t\t}")
    if target_trigger:
        parts.append("\t\ttarget_trigger = {")
        parts.extend(_indent(target_trigger, 3))
        parts.append("\t\t}")
    if state_target or target_root_trigger or target_trigger:
        parts.append("")

    if allowed or tag:
        parts.append("\t\tallowed = {")
        if allowed:
            parts.extend(_indent(allowed, 3))
        else:
            parts.append(f"\t\t\toriginal_tag = {tag}")
        parts.append("\t\t}")
        parts.append("")

    if visible:
        parts.append("\t\tvisible = {")
        parts.extend(_indent(visible, 3))
        parts.append("\t\t}")
        parts.append("")

    if available:
        parts.append("\t\tavailable = {")
        parts.extend(_indent(available, 3))
        parts.append("\t\t}")
        parts.append("")

    parts.append(f"\t\tcost = {cost}")
    if days_remove is not None:
        parts.append(f"\t\tdays_remove = {days_remove}")
    if days_re_enable is not None:
        parts.append(f"\t\tdays_re_enable = {days_re_enable}")
    parts.append("")

    if cancel_trigger:
        parts.append("\t\tcancel_trigger = {")
        parts.extend(_indent(cancel_trigger, 3))
        parts.append("\t\t}")
        parts.append("")

    parts.append("\t\tcomplete_effect = {")
    parts.append(f'\t\t\tlog = "[GetDateText]: [Root.GetName]: Decision {id} completed"')
    if complete_effect:
        parts.extend(_indent(complete_effect, 3))
    parts.append("\t\t}")

    if remove_effect:
        parts.append("")
        parts.append("\t\tremove_effect = {")
        parts.extend(_indent(remove_effect, 3))
        parts.append("\t\t}")

    parts.append("\t}")

    return {
        "txt": "\n".join(parts),
        "loc_yml_keys": [
            {"key": id, "value": title or id.replace("_", " ").title()},
            {"key": f"{id}_desc", "value": description or "TODO: decision description."},
        ],
    }


def _indent(block: str, tabs: int) -> List[str]:
    pad = "\t" * tabs
    return [pad + line if line.strip() else line for line in block.rstrip("\n").splitlines()]
