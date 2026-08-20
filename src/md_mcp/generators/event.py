"""Event scaffolder.

Generates a `country_event` / `news_event` / `state_event` block with the canonical
property order, plus matching loc keys (`ID.t`, `ID.d`, and `ID.<a|b|c|...>` per
option). The namespace declaration is also returned so the agent can prepend it
to the events file if missing.
"""

from __future__ import annotations

from typing import List, Optional


def generate_event(
    *,
    namespace: str,
    number: int,
    kind: str = "country_event",
    is_triggered_only: bool = True,
    fire_only_once: bool = False,
    picture: Optional[str] = None,
    trigger: Optional[str] = None,
    immediate: Optional[str] = None,
    options: Optional[List[dict]] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Scaffold one event block.

    Args:
      namespace          — file's `add_namespace = X` value (e.g. `Afghanistan`)
      number             — integer suffix (e.g. 3 → `Afghanistan.3`)
      kind               — `country_event`, `news_event`, `state_event`,
                           `unit_leader_event`, `operative_leader_event`
      is_triggered_only  — when true, omit `mean_time_to_happen`/`trigger`
      fire_only_once     — adds `fire_only_once = yes`
      picture            — GFX_ sprite (placeholder used if omitted)
      trigger            — raw paradox-script content for `trigger = { ... }`
      immediate          — raw paradox-script content for `immediate = { ... }`
      options            — list of `{name?: str, label?: str, ai_chance?: int,
                           effects?: str}`. Defaults to a single "Continue" option.
      title, description — loc values for `ID.t` / `ID.d`
    """
    if kind not in {
        "country_event",
        "news_event",
        "state_event",
        "unit_leader_event",
        "operative_leader_event",
    }:
        raise ValueError(f"Unsupported event kind: {kind}")

    eid = f"{namespace}.{number}"
    options = options or [{}]

    parts: List[str] = [f"{kind} = {{"]
    parts.append(f"\tid = {eid}")
    parts.append(f"\ttitle = {eid}.t")
    parts.append(f"\tdesc = {eid}.d")
    parts.append(f"\tpicture = GFX_{picture or 'event_generic'}")
    parts.append("")
    if is_triggered_only:
        parts.append("\tis_triggered_only = yes")
    if fire_only_once:
        parts.append("\tfire_only_once = yes")
    if is_triggered_only or fire_only_once:
        parts.append("")

    if trigger and not is_triggered_only:
        parts.append("\ttrigger = {")
        parts.extend(_indent(trigger, 2))
        parts.append("\t}")
        parts.append("")

    if immediate:
        parts.append("\timmediate = {")
        parts.extend(_indent(immediate, 2))
        parts.append("\t}")
        parts.append("")

    option_letters = "abcdefghijklmnopqrstuvwxyz"
    loc_keys: List[dict] = [
        {"key": f"{eid}.t", "value": title or f"Event: {namespace} {number}"},
        {"key": f"{eid}.d", "value": description or "TODO: event description."},
    ]

    for i, opt in enumerate(options):
        if i >= len(option_letters):
            raise ValueError("Too many options — only 26 supported")
        opt_id = f"{eid}.{option_letters[i]}"
        parts.append("\toption = {")
        parts.append(f"\t\tname = {opt_id}")
        parts.append(f'\t\tlog = "[GetDateText]: [This.GetName]: {opt_id} executed"')
        if opt.get("ai_chance") is not None:
            try:
                base = int(opt["ai_chance"])
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"Option {i} ai_chance is not an integer: {opt['ai_chance']!r}"
                ) from e
            parts.append(f"\t\tai_chance = {{ base = {base} }}")
        effects = opt.get("effects")
        if effects:
            parts.extend(_indent(effects, 2))
        parts.append("\t}")
        parts.append("")
        loc_keys.append({"key": opt_id, "value": opt.get("label") or "Continue"})

    if parts[-1] == "":
        parts.pop()
    parts.append("}")

    return {
        "txt": "\n".join(parts),
        "namespace_directive": f"add_namespace = {namespace}",
        "loc_yml_keys": loc_keys,
    }


def _indent(block: str, tabs: int) -> List[str]:
    pad = "\t" * tabs
    return [pad + line if line.strip() else line for line in block.rstrip("\n").splitlines()]
