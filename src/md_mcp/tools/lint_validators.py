"""Bridge between the lint dispatcher and the mod validator suite.

Maps changed-file paths to the validators whose domain covers them, runs the
selected validators through `ValidatorRunner.run()` (the single adapter point
for the brittle upstream API), and normalises issues into the lint shape.

Compatible collectors scope primary inputs to the relevant files before they
scan; definition lookups that pass `ignore_staged=True` still see the full repo.
`total_mod_wide` is the pre-filter count from the scan that ran; when a check
entry has `scoped: true`, that count is not a full-mod census. Collectors that
cannot take a file scope still scan their domain and are post-filtered.

`Issue.file` can't be compared to the scope set directly — it arrives as a
mod-relative path, a bare basename, `""`, or `"unknown"` depending on which
upstream check fired. `IssueAttributor` resolves each one to a real path first,
using the scan directories below to bound the search. What still won't resolve
is reported as an `unattributed` count plus a small sample, not merged whole: a
single fileless validator otherwise floods the response with issues that have
nothing to do with the edit.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Sequence

from ..analysis.suppressions import SUPPRESSION_SOURCE, suppressed_count
from ..util.encoding import read_text
from ..validators import SLOW_VALIDATORS, ValidatorRunner
from ..validators.attribution import IssueAttributor

# How many unattributable issues carry their detail into the response. The rest
# survive as a count on the check entry.
UNATTRIBUTED_SAMPLE = 5
STYLE_PREFIXES: tuple[str, ...] = ("common/", "events/", "history/", "music/")
EQUIPMENT_VARIANT_PREFIXES: tuple[str, ...] = ("common/", "events/", "history/")
# Match the paths that make upstream validate_equipment_variants expand its
# staged consumer scan. These files can change the assurance at an unchanged
# create_equipment_variant statement.
EQUIPMENT_VARIANT_CONTEXT_PREFIXES: tuple[str, ...] = (
    "common/technologies/",
    "common/technology_tags/",
    "common/bookmarks/",
    "history/countries/",
    "common/decisions/categories/",
    "common/national_focus/",
    "events/",
)
_EVENT_CALL_TOKENS = ("country_event", "news_event", "random_events")
_EVENT_LIST_ASSIGNMENT = re.compile(r"\bevents\s*=")
AUTO_ROUTING_EXCLUDED = frozenset({"common_mistakes"})

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
# style and equipment_variants have broad script domains but only inspect .txt
# files. Localisation's *.yml domain is extension-keyed too. These are handled
# as special cases below so non-script files don't select them.
#
# Deliberately absent: common_mistakes (lint_tool runs its dedicated checker),
# variables, set_variables, cosmetic_tags (global cross-reference scans,
# meaningless per-file) and the SLOW_VALIDATORS.
# All stay reachable by explicit name; other fast validators also run under "*".
VALIDATOR_AUTO_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "common/",
        (
            "agency_upgrades",
            "building_guards",
            "decisions",
            "dlc_guards",
            "events",
            "file_paths",
            "gfx_references",
            "ideas",
            "scripted_gui",
            "scripted_params",
            "simplifications",
            "tech_categories",
        ),
    ),
    (
        "events/",
        (
            "agency_upgrades",
            "building_guards",
            "characters",
            "decisions",
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
            "tech_categories",
        ),
    ),
    (
        "history/",
        (
            "agency_upgrades",
            "decisions",
            "events",
            "file_paths",
            "gfx_references",
            "history",
            "ideas",
            "oob_units",
            "scripted_params",
        ),
    ),
    (
        "localisation/",
        ("file_paths", "gfx_references", "ideas", "mios", "scripted_gui"),
    ),
    ("localisation/english/", ("decisions", "focus_tree")),
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
            "mios",
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
    ("common/ideas/", ("history", "ideas", "modifiers", "oob_units")),
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
    ("common/doctrines/", ("mios",)),
    # Decision category icons live here, not under interface/.
    ("gfx/interface/decisions/", ("decisions",)),
    # mios reads company traits and equipment stats for its bonus checks.
    ("common/country_leader/", ("characters", "mios")),
    ("common/units/equipment/", ("mios",)),
    ("common/equipment_groups/", ("mios",)),
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


def _scan_prefixes() -> dict[str, tuple[str, ...]]:
    """Invert VALIDATOR_AUTO_MAP: validator -> the directories it scans."""
    out: dict[str, set[str]] = {}
    for prefix, vals in VALIDATOR_AUTO_MAP:
        for v in vals:
            out.setdefault(v, set()).add(prefix)
    out.setdefault("localisation", set()).add("localisation/")
    out.setdefault("style", set()).update(STYLE_PREFIXES)
    out.setdefault("equipment_variants", set()).update(EQUIPMENT_VARIANT_PREFIXES)
    return {k: tuple(sorted(v)) for k, v in out.items()}


SCAN_PREFIXES: dict[str, tuple[str, ...]] = _scan_prefixes()


def _validators_for_path(path: str) -> set[str]:
    names: set[str] = set()
    if (
        path.startswith("localisation/")
        and path.endswith(".yml")
        and not path.startswith("localisation/english/")
    ):
        # Upstream intentionally audits English localisation only. Explicit
        # validators remain an opt-in escape hatch; auto routing must not flag
        # expected non-English drift.
        return names
    for prefix, vals in VALIDATOR_AUTO_MAP:
        if path.startswith(prefix):
            names.update(vals)
    if path.startswith("localisation/") and path.endswith(".yml"):
        names.add("localisation")
    # Decision icon checks read interface gfx, not every .gui file.
    if path.startswith("interface/") and path.endswith(".gfx"):
        names.add("decisions")
    # style scans every .txt in the script dirs; catch-all so any script edit
    # gets a style pass.
    if path.endswith(".txt") and path.startswith(STYLE_PREFIXES):
        names.add("style")
    if path.endswith(".txt") and path.startswith(EQUIPMENT_VARIANT_PREFIXES):
        names.add("equipment_variants")
    return names


def _load_upstream_module(mod_root: Path, relative: str, module_name: str):
    """Load one of MD's routing modules without adding its checkout to sys.path."""
    source = mod_root / relative
    if not source.is_file():
        return None
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # dataclasses consult sys.modules while decorating ValidatorSpec.
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    return module


@lru_cache(maxsize=8)
def _upstream_routing(mod_root_text: str):
    """Return the current checkout's validator/group contract, if available.

    The fallback preserves compatibility with older or partial checkouts that
    predate the shared CI routing modules.
    """
    root = Path(mod_root_text)
    batches = _load_upstream_module(
        root, "tools/validation/validator_batches.py", f"md_batches_{hash(root)}"
    )
    groups = _load_upstream_module(
        root, "tools/validation/change_groups.py", f"md_groups_{hash(root)}"
    )
    if batches is None or groups is None:
        return None
    return batches, groups


def _upstream_excluded_names(routing) -> set[str]:
    batches, _groups = routing
    excluded_scripts = getattr(batches, "_IMPACT_EXCLUDED_SCRIPTS", ())
    return {
        Path(script).stem.removeprefix("validate_").replace("-", "_") for script in excluded_scripts
    }


def _upstream_validators_for_paths(
    paths: list[str], mod_root: Path
) -> tuple[set[str], dict[str, tuple[str, ...]]] | None:
    """Resolve content changes using upstream's current group/spec definitions."""
    routing = _upstream_routing(str(mod_root.resolve()))
    if routing is None:
        return None
    batches, groups = routing
    normalized = [path.replace("\\", "/") for path in paths]
    # Changed tooling code uses MD's impact selector, which owns the import
    # graph, broad shared-tool rules, and impact-only checks.
    if any(path.startswith("tools/") for path in normalized):
        selected, adhoc = batches.select_for_changed_files(normalized)
        specs = [*selected, *adhoc]
        by_name = {
            Path(spec.script).stem.removeprefix("validate_").replace("-", "_"): tuple(spec.args)
            for spec in specs
        }
        return set(by_name) - AUTO_ROUTING_EXCLUDED, by_name

    changed = groups.classify(normalized)
    changed_groups = {name for name, value in changed.items() if value is True}
    selected_names: set[str] = set()
    args_by_name: dict[str, tuple[str, ...]] = {}
    for spec in batches.ALL_SPECS:
        name = Path(spec.script).stem.removeprefix("validate_").replace("-", "_")
        if changed_groups.intersection(spec.groups):
            selected_names.add(name)
            args_by_name[name] = tuple(spec.args)

    # CI's impact-only validators do not have content groups. Keep their
    # defined special cases in sync with change_groups' classification output.
    if changed.get("file-paths"):
        selected_names.add("file_paths")
    if changed.get("style"):
        selected_names.add("style")
    if "descriptor.mod" in normalized or any(p.endswith(".mod") for p in normalized):
        selected_names.add("mod_descriptors")
    for spec in batches.IMPACT_ONLY_SPECS:
        name = Path(spec.script).stem.removeprefix("validate_").replace("-", "_")
        if name in selected_names:
            args_by_name[name] = tuple(spec.args)

    selected_names -= _upstream_excluded_names(routing)
    return selected_names, args_by_name


def _has_event_call(text: str) -> bool:
    return any(token in text for token in _EVENT_CALL_TOKENS) or bool(
        _EVENT_LIST_ASSIGNMENT.search(text)
    )


def _equipment_variant_context_changed(relevant: set, mod_root: Optional[Path]) -> bool:
    """Whether a scoped edit can change availability at an unchanged consumer."""
    for path in relevant:
        path = path.replace("\\", "/")
        if not path.endswith(".txt") or not path.startswith(EQUIPMENT_VARIANT_PREFIXES):
            continue
        if path.startswith(EQUIPMENT_VARIANT_CONTEXT_PREFIXES):
            return True
        if mod_root is None:
            continue
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        try:
            text = read_text(mod_root / relative)
            if _has_event_call(text):
                return True
        except OSError:
            pass
        # A removed final event call still changes context, although the
        # current file no longer contains a token for the check above to find.
        try:
            diff = subprocess.run(
                ["git", "diff", "--no-ext-diff", "--unified=0", "HEAD", "--", path],
                cwd=mod_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if diff.returncode == 0 and any(
            line.startswith("-") and not line.startswith("---") and _has_event_call(line)
            for line in diff.stdout.splitlines()
        ):
            return True
    return False


def _upstream_args(mod_root: Optional[Path]) -> dict[str, tuple[str, ...]]:
    if mod_root is None:
        return {}
    routing = _upstream_routing(str(mod_root.resolve()))
    if routing is None:
        return {}
    batches, _groups = routing
    specs = [*batches.ALL_SPECS, *batches.IMPACT_ONLY_SPECS]
    return {
        Path(spec.script).stem.removeprefix("validate_").replace("-", "_"): tuple(spec.args)
        for spec in specs
        if spec.args
    }


def select_validators(
    relevant: Optional[list[str]], available: set[str], mod_root: Optional[Path] = None
) -> list[str]:
    """Resolve `validators=["auto"]` to concrete names for the given file scope.

    `relevant=None` (mode=all) degrades to every fast auto-routable validator.
    An empty relevant list selects nothing — zero runner calls on a clean tree.
    """
    if mod_root is not None:
        routing = _upstream_routing(str(mod_root.resolve()))
        if routing is not None:
            excluded = _upstream_excluded_names(routing) | AUTO_ROUTING_EXCLUDED
            if relevant is None:
                return sorted(available - SLOW_VALIDATORS - excluded)
            upstream = _upstream_validators_for_paths(relevant, mod_root)
            if upstream is not None:
                upstream_names, _args = upstream
                return sorted((upstream_names - SLOW_VALIDATORS - excluded) & available)
    if relevant is None:
        return sorted(available - SLOW_VALIDATORS - AUTO_ROUTING_EXCLUDED)
    wanted: set[str] = set()
    for f in relevant:
        wanted |= _validators_for_path(f)
    return sorted((wanted - AUTO_ROUTING_EXCLUDED) & available)


def run_validators_for_lint(
    runner: ValidatorRunner,
    names: Sequence[str],
    *,
    staged_only: bool,
    relevant_set: Optional[set],
    mod_root: Optional[Path] = None,
) -> tuple[list[dict], list[dict]]:
    """Run validators and normalise output into the lint dispatcher's shape.

    Returns (check_entries, issues). Check entries are named `validator:<name>`
    and carry `total` (on-scope only), plus `total_mod_wide` and, when any
    survive, `related` or `unattributed`. A `scoped: true` entry means the
    collector limited primary inputs, so `total_mod_wide` is not a full-mod
    census. The returned issue list also appends up to `UNATTRIBUTED_SAMPLE`
    unattributed issues (tagged `scope="unattributed"`), and `lint_tool` folds
    those into its top-level counts, so when unattributed issues exist `total`
    is the on-scope count, not the length of the returned list. Per-validator
    failures are isolated, same as lint checks.

    Without `mod_root` there's nothing to resolve partial paths against, so
    matching degrades to exact comparison.
    """
    wanted = {os.path.normpath(f) for f in relevant_set} if relevant_set is not None else None
    attributor = IssueAttributor(mod_root) if mod_root is not None else None
    context_changed = (
        _equipment_variant_context_changed(relevant_set, mod_root)
        if relevant_set is not None and "equipment_variants" in names
        else False
    )
    args_by_name = _upstream_args(mod_root)

    check_entries: list[dict] = []
    issues_out: list[dict] = []
    related_out: list[dict] = []
    for name in names:
        label = f"validator:{name}"
        try:
            # Upstream's staged shortcut only detects event tokens still in the
            # edited file. Full scan also catches removed event-call context,
            # so file scoping is skipped too: the related issues live elsewhere.
            full_scan = name == "equipment_variants" and context_changed
            use_staged = staged_only and not full_scan
            run_options: dict[str, Any] = {"staged_only": use_staged}
            validator_args = args_by_name.get(name, ())
            if validator_args:
                run_options["args"] = validator_args
            if wanted is not None and not full_scan:
                result = runner.run(
                    name,
                    files=sorted(wanted),
                    post_filter=False,
                    **run_options,
                )
            else:
                result = runner.run(name, **run_options)
        except Exception as e:
            check_entries.append({"name": label, "ok": False, "error": str(e)})
            continue
        if not result.get("ok"):
            check_entries.append({"name": label, "ok": False, "error": result.get("error")})
            continue

        raw = list(result.get("issues", []) or [])
        suppressed = suppressed_count(result)
        prefixes = SCAN_PREFIXES.get(name, ())
        on_scope: list[dict] = []
        related: list[dict] = []
        unattributed: list[dict] = []
        if wanted is not None:
            for i in raw:
                resolved = _resolve(i, attributor, prefixes)
                if resolved is None:
                    unattributed.append(i)
                elif os.path.normpath(resolved) in wanted:
                    on_scope.append(dict(i, file=resolved))
                elif name == "equipment_variants" and context_changed:
                    related.append(dict(i, file=resolved))
        else:
            on_scope = raw

        entry = {"name": label, "ok": True, "total": len(on_scope)}
        if suppressed:
            entry["suppressed_mod_wide"] = suppressed
            entry["suppression_source"] = SUPPRESSION_SOURCE
        if wanted is not None:
            entry["total_mod_wide"] = len(raw) + suppressed
            if result.get("scoped"):
                entry["scoped"] = True
            if related:
                entry["related"] = len(related)
            if unattributed:
                entry["unattributed"] = len(unattributed)
        check_entries.append(entry)

        for i in on_scope:
            issues_out.append(_normalise(i, label))
        for i in related:
            related_out.append({**_normalise(i, label), "scope": "related"})
        for i in unattributed[:UNATTRIBUTED_SAMPLE]:
            issues_out.append({**_normalise(i, label), "scope": "unattributed"})

    return check_entries, issues_out + related_out


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
