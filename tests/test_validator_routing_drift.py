"""Integration contract for upstream validator routing."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

from md_mcp.tools.lint_validators import SCAN_PREFIXES, _validators_for_path
from md_mcp.validators import SLOW_VALIDATORS

EXPECTED_REGISTRY = {
    "validate_style": (("", ".txt"),),
    "validate_oob_units": (
        ("history/units/", ".txt"),
        ("common/units/", ".txt"),
        ("common/ai_templates/", ".txt"),
        ("common/scripted_effects/", ".txt"),
        ("history/countries/", ".txt"),
        ("common/national_focus/", ".txt"),
        ("events/", ".txt"),
        ("common/decisions/", ".txt"),
        ("common/special_projects/", ".txt"),
        ("common/on_actions/", ".txt"),
        ("common/operations/", ".txt"),
        ("common/resistance_compliance_modifiers/", ".txt"),
        ("common/scripted_guis/", ".txt"),
    ),
    "validate_ai_roles": (
        ("common/ai_strategy/", ".txt"),
        ("common/ai_templates/", ".txt"),
    ),
    "validate_ai_navy": (("common/ai_navy/", ".txt"), ("common/units/", ".txt")),
    "validate_characters": (
        ("common/characters/", ".txt"),
        ("common/unit_leader/", ".txt"),
        ("common/national_focus/", ".txt"),
        ("common/decisions/", ".txt"),
        ("common/scripted_effects/", ".txt"),
        ("common/on_actions/", ".txt"),
        ("events/", ".txt"),
        ("history/countries/", ".txt"),
    ),
    "validate_ai_equipment": (("common/ai_equipment/", ".txt"),),
    "validate_agency_upgrades": (
        ("common/intelligence_agency_upgrades/", ".txt"),
        ("common/on_actions/MD_auto_agency_on_actions.txt", ""),
        ("common/scripted_guis/00_MD_auto_agency_scripted_gui.txt", ""),
        ("localisation/english/MD_auto_agency_l_english.yml", ""),
    ),
    "validate_ideas": (
        ("common/ideas/", ".txt"),
        ("common/national_focus/", ".txt"),
        ("common/decisions/", ".txt"),
        ("common/on_actions/", ".txt"),
        ("common/scripted_effects/", ".txt"),
        ("common/scripted_triggers/", ".txt"),
        ("events/", ".txt"),
        ("history/", ".txt"),
        ("localisation/english/", ".yml"),
    ),
    "validate_events": (("common/", ".txt"), ("events/", ".txt"), ("history/", ".txt")),
    "validate_mios": (
        ("common/military_industrial_organization/organizations/", ".txt"),
        ("localisation/english/", ".yml"),
    ),
}

EXPECTED_REGISTRY_EXCLUDES = {
    "validate_style": r"Changelog\.txt$|AUTHORS\.txt$|descriptions.*\.txt$"
}

CORE_GROUPS = ("common", "events", "history", "interface", "localisation")
EXPECTED_CI_ROUTING = {
    "agency_upgrades": CORE_GROUPS,
    "ai_equipment": ("ai-equipment",),
    "ai_navy": ("ai-navy",),
    "ai_roles": ("ai-strategy",),
    "characters": ("characters",),
    "cosmetic_tags": CORE_GROUPS,
    "decisions": ("decisions",),
    "defines": CORE_GROUPS,
    "dlc_guards": ("common", "events"),
    "events": CORE_GROUPS,
    "factions": ("factions",),
    "focus_tree": ("national-focus",),
    "gfx_references": ("common", "events", "history", "interface", "localisation"),
    "history": CORE_GROUPS,
    "ideas": CORE_GROUPS,
    "localisation": CORE_GROUPS,
    "mios": ("localisation", "mios"),
    "modifiers": ("common",),
    "on_actions": ("events", "on-actions"),
    "oob_units": ("oob",),
    "scientist_traits": ("scientist-traits",),
    "scripted_gui": ("interface", "scripted-guis"),
    "scripted_localisation": CORE_GROUPS,
    "scripted_params": ("decisions", "events", "national-focus", "scripted-effects"),
    "set_variables": CORE_GROUPS,
    "simplifications": (
        "decisions",
        "events",
        "national-focus",
        "on-actions",
        "scripted-effects",
    ),
    "technologies": ("common",),
    "unused_scripted": CORE_GROUPS,
    "variables": CORE_GROUPS,
}

EXPECTED_CI_FILTERS = {
    "ai-equipment": ("common/ai_equipment/**",),
    "ai-navy": ("common/ai_navy/**", "common/units/**"),
    "ai-strategy": ("common/ai_strategy/**", "common/ai_templates/**"),
    "characters": (
        "common/characters/**",
        "common/unit_leader/**",
        "common/national_focus/**",
        "common/decisions/**",
        "common/scripted_effects/**",
        "common/on_actions/**",
        "events/**",
        "history/countries/**",
    ),
    "common": ("common/**",),
    "decisions": ("common/**/*.txt", "events/**/*.txt", "history/**/*.txt"),
    "events": ("events/**",),
    "factions": ("common/factions/**",),
    "history": ("history/**",),
    "interface": ("interface/**",),
    "localisation": ("localisation/**",),
    "mios": ("common/military_industrial_organization/**",),
    "national-focus": ("common/national_focus/**",),
    "on-actions": ("common/on_actions/**",),
    "oob": (
        "history/units/**",
        "common/units/**",
        "common/ai_templates/**",
        "common/scripted_effects/**",
        "history/countries/**",
        "common/national_focus/**",
        "events/**",
        "common/decisions/**",
        "common/special_projects/**",
        "common/on_actions/**",
        "common/operations/**",
        "common/resistance_compliance_modifiers/**",
        "common/scripted_guis/**",
    ),
    "scientist-traits": ("common/scientist_traits/**", "interface/**"),
    "scripted-effects": ("common/scripted_effects/**",),
    "scripted-guis": ("common/scripted_guis/**",),
}

INTENTIONALLY_NOT_AUTO_ROUTED = {
    "cosmetic_tags",
    "set_variables",
    "unused_scripted",
    "variables",
} | SLOW_VALIDATORS


def _load_registry(mod_root: Path):
    path = mod_root / "tools" / "precommit_validate.py"
    spec = importlib.util.spec_from_file_location("md_upstream_precommit_validate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._REGISTRY


def _load_ci_routes(mod_root: Path):
    path = mod_root / ".github" / "workflows" / "coding-pipeline.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    filter_step = next(
        step for step in jobs["detect-changes"]["steps"] if step.get("id") == "filter"
    )
    filters = yaml.safe_load(filter_step["with"]["filters"])

    routes = {}
    for job_name in ("validate-core", "validate-targeted"):
        job = jobs[job_name]
        job_groups = set(re.findall(r"outputs\.([a-z-]+)", job["if"]))
        for entry in job["strategy"]["matrix"]["validator"]:
            name = Path(entry["script"]).stem.removeprefix("validate_")
            expression = entry.get("should_run")
            groups = (
                job_groups
                if expression is None
                else set(re.findall(r"outputs\.([a-z-]+)", expression))
            )
            routes[name] = tuple(sorted(groups))
    return routes, filters


@pytest.mark.integration
def test_upstream_commit_routing_matches_auto_map(real_mod_root):
    registry = _load_registry(real_mod_root)
    routes = {spec.script: tuple(spec.rules) for spec in registry}
    excludes = {spec.script: spec.exclude.pattern for spec in registry if spec.exclude}
    assert routes == EXPECTED_REGISTRY
    assert excludes == EXPECTED_REGISTRY_EXCLUDES

    for spec in registry:
        name = spec.script.removeprefix("validate_")
        if name == "style":
            continue
        for prefix, extension in spec.rules:
            probe = prefix if not extension else f"{prefix}__routing_probe{extension}"
            assert name in _validators_for_path(probe), f"{name} is not auto-routed for {probe}"


@pytest.mark.integration
def test_upstream_ci_routing_matches_auto_map(real_mod_root):
    routes, filters = _load_ci_routes(real_mod_root)
    assert routes == EXPECTED_CI_ROUTING
    assert {name: tuple(filters[name]) for name in EXPECTED_CI_FILTERS} == EXPECTED_CI_FILTERS

    missing = set(routes) - set(SCAN_PREFIXES) - INTENTIONALLY_NOT_AUTO_ROUTED
    assert not missing, f"CI validators missing from auto routing: {sorted(missing)}"
