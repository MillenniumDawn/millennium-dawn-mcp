"""Bridge between the lint dispatcher and the mod validator suite.

Maps changed-file paths to the validators whose domain covers them, runs the
selected validators through `ValidatorRunner.run()` (the single adapter point
for the brittle upstream API), and normalises issues into the lint shape.

Validators scan their whole domain regardless of scope; we post-filter issues
by the relevant-file set and report both on-scope and mod-wide totals so scope
filtering never silently hides cross-file breakage.

`Issue.file` can't be compared to the scope set directly — it arrives as a
mod-relative path, a bare basename, `""`, or `"unknown"` depending on which
upstream check fired. `IssueAttributor` resolves each one to a real path first,
using the scan directories below to bound the search. What still won't resolve
is reported as an `unattributed` count plus a small sample, not merged whole: a
single fileless validator otherwise floods the response with issues that have
nothing to do with the edit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..validators import SLOW_VALIDATORS, ValidatorRunner
from ..validators.attribution import IssueAttributor

# How many unattributable issues carry their detail into the response. The rest
# survive as a count on the check entry.
UNATTRIBUTED_SAMPLE = 5

# Path-prefix -> validators whose scan domain covers that directory. A file can
# match several rows; matches union. Derived from the scan globs in
# Millennium-Dawn/tools/validation/validate_*.py: self._collect_files(...)
# calls, module-level SCAN_PATTERNS-style constants, and direct glob.glob(...)
# calls — verified against source, not inferred from directory naming.
#
# Several validators scan the whole common/, events/, and/or history/ tree
# recursively in addition to their "obvious" subdirectory — e.g. gfx_references
# resolves GFX references out of every script file, not just interface/. Those
# get their own broad row here and compose with the narrower rows via union.
# style's equally broad common/events/history domain and localisation's *.yml
# domain are already handled as extension-keyed special cases below, so they
# don't need a row here too.
#
# Deliberately absent: variables, set_variables, cosmetic_tags (global
# cross-reference scans, meaningless per-file) and the SLOW_VALIDATORS.
# All stay reachable by explicit name; the fast globals also run under "*".
VALIDATOR_AUTO_MAP: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "common/",
        (
            "agency_upgrades",
            "dlc_guards",
            "events",
            "file_paths",
            "gfx_references",
            "ideas",
            "scripted_gui",
            "simplifications",
        ),
    ),
    (
        "events/",
        (
            "agency_upgrades",
            "characters",
            "dlc_guards",
            "events",
            "file_paths",
            "focus_tree",
            "gfx_references",
            "ideas",
            "oob_units",
            "on_actions",
            "scripted_gui",
            "scripted_params",
            "simplifications",
        ),
    ),
    ("history/", ("agency_upgrades", "events", "file_paths", "gfx_references", "history", "ideas")),
    (
        "localisation/",
        ("file_paths", "gfx_references", "ideas", "mios", "scripted_gui"),
    ),
    (
        "localisation/english/MD_auto_agency_l_english.yml",
        ("agency_upgrades",),
    ),
    (
        "interface/",
        (
            "agency_upgrades",
            "factions",
            "file_paths",
            "gfx_references",
            "ideas",
            "scientist_traits",
            "scripted_gui",
            "scripted_localisation",
        ),
    ),
    (
        "common/national_focus/",
        (
            "characters",
            "focus_tree",
            "modifiers",
            "oob_units",
            "scripted_params",
            "simplifications",
        ),
    ),
    (
        "common/on_actions/",
        ("characters", "events", "on_actions", "oob_units", "simplifications"),
    ),
    (
        "common/decisions/",
        (
            "characters",
            "decisions",
            "modifiers",
            "oob_units",
            "scripted_params",
            "simplifications",
        ),
    ),
    ("common/bop/", ("decisions",)),
    ("common/ideas/", ("history", "ideas", "modifiers")),
    ("common/characters/", ("characters", "ideas")),
    ("common/unit_leader/", ("characters",)),
    ("common/dynamic_modifiers/", ("modifiers",)),
    ("common/modifier_definitions/", ("modifiers",)),
    ("common/idea_tags/", ("modifiers",)),
    ("common/operations/", ("modifiers", "oob_units")),
    (
        "common/scripted_effects/",
        ("characters", "focus_tree", "oob_units", "scripted_params", "simplifications"),
    ),
    ("common/scripted_triggers/", ("simplifications",)),
    ("common/scripted_guis/", ("scripted_gui", "gfx_references", "oob_units")),
    ("common/scripted_localisation/", ("decisions", "scripted_localisation", "gfx_references")),
    ("common/ai_strategy/", ("ai_roles",)),
    ("common/ai_templates/", ("ai_roles", "oob_units")),
    ("common/ai_equipment/", ("ai_equipment", "ai_roles")),
    ("common/ai_navy/", ("ai_navy",)),
    ("common/units/", ("ai_equipment", "ai_navy", "modifiers", "oob_units")),
    ("common/units/names/", ("oob_units",)),
    ("common/units/names_ships/", ("oob_units",)),
    ("common/units/names_divisions/", ("oob_units",)),
    ("common/intelligence_agency_upgrades/", ("agency_upgrades",)),
    ("common/special_projects/", ("history", "oob_units")),
    ("common/technologies/", ("history", "technologies")),
    ("common/military_industrial_organization/", ("mios",)),
    ("common/scientist_traits/", ("scientist_traits",)),
    ("common/factions/", ("factions",)),
    ("common/defines/MD_defines.lua", ("defines",)),
    ("descriptor.mod", ("mod_descriptors",)),
    ("Millennium_Dawn.mod", ("mod_descriptors",)),
    ("history/units/", ("oob_units",)),
    ("history/countries/", ("characters", "oob_units")),
    ("history/states/", ("history",)),
    ("common/resistance_compliance_modifiers/", ("oob_units",)),
    ("descriptions/", ("file_paths",)),
    ("gfx/", ("file_paths",)),
    ("map/", ("file_paths",)),
    ("music/", ("file_paths",)),
    ("portraits/", ("file_paths",)),
    ("scenario_tests/", ("file_paths",)),
    ("sound/", ("file_paths",)),
    ("tutorial/", ("file_paths",)),
)


def _scan_prefixes() -> Dict[str, Tuple[str, ...]]:
    """Invert VALIDATOR_AUTO_MAP: validator -> the directories it scans."""
    out: Dict[str, Set[str]] = {}
    for prefix, vals in VALIDATOR_AUTO_MAP:
        for v in vals:
            out.setdefault(v, set()).add(prefix)
    out.setdefault("localisation", set()).add("localisation/")
    out.setdefault("style", set()).update({"common/", "events/", "history/"})
    return {k: tuple(sorted(v)) for k, v in out.items()}


SCAN_PREFIXES: Dict[str, Tuple[str, ...]] = _scan_prefixes()


def _validators_for_path(path: str) -> Set[str]:
    names: Set[str] = set()
    for prefix, vals in VALIDATOR_AUTO_MAP:
        if path.startswith(prefix):
            names.update(vals)
    if path.startswith("localisation/") and path.endswith(".yml"):
        names.add("localisation")
    # style scans every .txt in the script dirs; catch-all so any script edit
    # gets a style pass.
    if path.endswith(".txt") and path.startswith(("common/", "events/", "history/")):
        names.add("style")
    return names


def select_validators(relevant: Optional[List[str]], available: Set[str]) -> List[str]:
    """Resolve `validators=["auto"]` to concrete names for the given file scope.

    `relevant=None` (mode=all) degrades to every fast validator. An empty
    relevant list selects nothing — zero runner calls on a clean tree.
    """
    if relevant is None:
        return sorted(available - SLOW_VALIDATORS)
    wanted: Set[str] = set()
    for f in relevant:
        wanted |= _validators_for_path(f)
    return sorted(wanted & available)


def run_validators_for_lint(
    runner: ValidatorRunner,
    names: Sequence[str],
    *,
    staged_only: bool,
    relevant_set: Optional[set],
    mod_root: Optional[Path] = None,
) -> Tuple[List[dict], List[dict]]:
    """Run validators and normalise output into the lint dispatcher's shape.

    Returns (check_entries, issues). Check entries are named `validator:<name>`
    and carry `total` (on-scope only), plus `total_mod_wide` and, when any
    survive, `unattributed`. The returned issue list also appends up to
    `UNATTRIBUTED_SAMPLE` unattributed issues (tagged `scope="unattributed"`),
    and `lint_tool` folds those into its top-level counts, so when unattributed
    issues exist `total` is the on-scope count, not the length of the returned
    list. Per-validator failures are isolated, same as lint checks.

    Without `mod_root` there's nothing to resolve partial paths against, so
    matching degrades to exact comparison.
    """
    wanted = {os.path.normpath(f) for f in relevant_set} if relevant_set is not None else None
    attributor = IssueAttributor(mod_root) if mod_root is not None else None

    check_entries: List[dict] = []
    issues_out: List[dict] = []
    for name in names:
        label = f"validator:{name}"
        try:
            result = runner.run(name, staged_only=staged_only)
        except Exception as e:
            check_entries.append({"name": label, "ok": False, "error": str(e)})
            continue
        if not result.get("ok"):
            check_entries.append({"name": label, "ok": False, "error": result.get("error")})
            continue

        raw = result.get("issues", []) or []
        prefixes = SCAN_PREFIXES.get(name, ())
        on_scope: List[dict] = []
        unattributed: List[dict] = []
        if wanted is not None:
            for i in raw:
                resolved = _resolve(i, attributor, prefixes)
                if resolved is None:
                    unattributed.append(i)
                elif os.path.normpath(resolved) in wanted:
                    on_scope.append(dict(i, file=resolved))
        else:
            on_scope = raw

        entry = {"name": label, "ok": True, "total": len(on_scope)}
        if wanted is not None:
            entry["total_mod_wide"] = len(raw)
            if unattributed:
                entry["unattributed"] = len(unattributed)
        check_entries.append(entry)

        for i in on_scope:
            issues_out.append(_normalise(i, label))
        for i in unattributed[:UNATTRIBUTED_SAMPLE]:
            issues_out.append({**_normalise(i, label), "scope": "unattributed"})

    return check_entries, issues_out


def _resolve(
    issue: dict, attributor: Optional[IssueAttributor], prefixes: Sequence[str]
) -> Optional[str]:
    if attributor is not None:
        return attributor.resolve(issue, scan_prefixes=prefixes)
    f = (issue.get("file") or "").strip()
    return f or None


def _normalise(issue: dict, label: str) -> dict:
    norm = {
        "check": label,
        "file": issue.get("file"),
        "message": issue.get("message"),
        "severity": issue.get("severity", "info"),
    }
    if issue.get("line"):
        norm["line"] = issue["line"]
    if issue.get("category"):
        norm["category"] = issue["category"]
    return norm
