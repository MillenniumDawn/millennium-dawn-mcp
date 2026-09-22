"""Integration contract for upstream validator routing."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
import yaml

from md_mcp.tools.lint_validators import SCAN_PREFIXES, _validators_for_path
from md_mcp.validators import SLOW_VALIDATORS

EXPECTED_REGISTRY = {
    "validate_common_mistakes": (("", ".txt"),),
    "validate_style": (("", ".txt"),),
    "validate_oob_units": (
        ("history/", ".txt"),
        ("common/units/", ".txt"),
        ("common/ai_templates/", ".txt"),
        ("common/scripted_effects/", ".txt"),
        ("common/national_focus/", ".txt"),
        ("events/", ".txt"),
        ("common/decisions/", ".txt"),
        ("common/special_projects/", ".txt"),
        ("common/on_actions/", ".txt"),
        ("common/operations/", ".txt"),
        ("common/resistance_compliance_modifiers/", ".txt"),
        ("common/scripted_guis/", ".txt"),
        ("common/ideas/", ".txt"),
    ),
    "validate_ai_roles": (
        ("common/ai_strategy/", ".txt"),
        ("common/ai_templates/", ".txt"),
    ),
    "validate_ai_navy": (("common/ai_navy/", ".txt"), ("common/units/", ".txt")),
    "validate_characters": (
        ("common/characters/", ".txt"),
        ("common/unit_leader/", ".txt"),
        ("common/country_leader/", ".txt"),
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
        ("common/idea_tags/", ".txt"),
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
        ("common/military_industrial_organization/policies/", ".txt"),
        ("common/country_leader/", ".txt"),
        ("common/doctrines/", ".txt"),
        ("common/units/equipment/", ".txt"),
        ("common/equipment_groups/", ".txt"),
        ("interface/", ".gfx"),
        ("localisation/english/", ".yml"),
    ),
}

EXPECTED_REGISTRY_EXCLUDES = {
    "validate_common_mistakes": r"Changelog\.txt$|AUTHORS\.txt$|descriptions.*\.txt$",
    "validate_style": r"Changelog\.txt$|AUTHORS\.txt$|descriptions.*\.txt$",
}

CORE_GROUPS = ("common", "events", "history", "interface", "localisation", "map-adjacency")
EXPECTED_CI_ROUTING = {
    "achievements": CORE_GROUPS,
    "agency_upgrades": CORE_GROUPS,
    "ai_equipment": ("ai-equipment",),
    "ai_navy": ("ai-navy",),
    "ai_path_rules": ("common", "history", "national-focus"),
    "ai_roles": ("ai-strategy",),
    "bonus_names": ("common", "events"),
    "building_guards": ("common", "events"),
    "characters": ("characters",),
    "common_mistakes": CORE_GROUPS,
    "cosmetic_tags": CORE_GROUPS,
    "country_names": ("common",),
    "decisions": ("decisions", "localisation"),
    "defines": CORE_GROUPS,
    "dlc_guards": ("common", "events"),
    "dynamic_modifier_guards": ("common", "events"),
    "equipment_upkeep": ("oob",),
    "equipment_variants": ("common", "events", "history"),
    "events": CORE_GROUPS,
    "factions": ("factions",),
    "focus_tree": ("localisation", "national-focus"),
    "gfx_references": ("common", "events", "history", "interface", "localisation"),
    "history": CORE_GROUPS,
    "ideas": CORE_GROUPS,
    "influence_calls": ("common", "events"),
    "localisation": CORE_GROUPS,
    "math_expressions": CORE_GROUPS,
    "mio_icons": ("mios",),
    "mios": ("interface", "localisation", "mios"),
    "modifiers": ("common",),
    "on_actions": ("events", "on-actions"),
    "oob_units": ("oob",),
    "party_loc": ("common", "localisation"),
    "scientist_traits": ("scientist-traits",),
    "scripted_gui": ("interface", "scripted-guis"),
    "scripted_localisation": CORE_GROUPS,
    "scripted_params": ("common", "events", "history"),
    "set_variables": CORE_GROUPS,
    "simplifications": (
        "decisions",
        "events",
        "national-focus",
        "on-actions",
        "scripted-effects",
    ),
    "tech_categories": ("common", "events"),
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
        "common/country_leader/**",
        "common/national_focus/**",
        "common/decisions/**",
        "common/scripted_effects/**",
        "common/on_actions/**",
        "events/**",
        "history/countries/**",
    ),
    "common": ("common/**",),
    "decisions": (
        "common/**/*.txt",
        "events/**/*.txt",
        "history/**/*.txt",
        "interface/**/*.gfx",
        "gfx/interface/decisions/**",
    ),
    "events": ("events/**",),
    "factions": ("common/factions/**",),
    "history": ("history/**",),
    "interface": ("interface/**",),
    "localisation": ("localisation/**",),
    "map-adjacency": ("map/adjacency_rules.txt",),
    "mios": (
        "common/military_industrial_organization/**",
        "common/country_leader/**",
        "common/doctrines/**",
        "common/units/equipment/**",
        "common/equipment_groups/**",
        "interface/**",
    ),
    "national-focus": ("common/national_focus/**",),
    "on-actions": ("common/on_actions/**",),
    "oob": (
        "history/units/**",
        "history/**",
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
        "common/ideas/**",
    ),
    "scientist-traits": ("common/scientist_traits/**", "interface/**"),
    "scripted-effects": ("common/scripted_effects/**",),
    "scripted-guis": ("common/scripted_guis/**",),
    "scripted-loc": ("common/scripted_localisation/**",),
    "style": (
        "common/**/*.txt",
        "events/**/*.txt",
        "history/**/*.txt",
        "music/**/*.txt",
    ),
    "mod": ("*.mod",),
    "content": (
        "common/**",
        "events/**",
        "history/**",
        "localisation/**",
        "interface/**",
        "gfx/interface/decisions/**",
        "music/**",
        "map/adjacency_rules.txt",
        "*.mod",
    ),
    "docs": ("docs/**", "tools/docs_checks/**", ".github/workflows/docs-quality.yml"),
}

EXPECTED_WORKSPACE_PATHS = (
    "common",
    "events",
    "history",
    "localisation",
    "interface",
    "gfx/flags",
    "gfx/interface/decisions",
    "map/adjacency_rules.txt",
    "music",
    "tools",
    "resources/documentation",
    ".claude",
    ".github/actions/setup-md-python/action.yml",
    "CLAUDE.md",
    "pyproject.toml",
    "*.mod",
    ".workspace-manifest",
    ".validation_cache",
)
EXPECTED_PREPARE_WORKSPACE_PATHS = (
    "common",
    "events",
    "history",
    "localisation",
    "interface",
    "gfx/flags",
    "gfx/interface/decisions",
    "map/adjacency_rules.txt",
    "music",
    "tools",
    "resources/documentation",
    ".claude",
    ".github/actions/setup-md-python/action.yml",
    "CLAUDE.md",
    "pyproject.toml",
    "*.mod",
)
EXPECTED_VALIDATE_PATHS_CHECKOUT = (
    "descriptor.mod",
    "tools",
    ".github/actions/setup-md-python/action.yml",
)

# These CI gates intentionally cover more paths than the validator scans.
COARSE_CI_ROUTES = {
    (validator, group)
    for validator, groups in EXPECTED_CI_ROUTING.items()
    if groups == CORE_GROUPS
    for group in groups
} | {
    ("modifiers", "common"),
    ("scripted_params", "decisions"),
    ("simplifications", "decisions"),
    ("technologies", "common"),
}

# CI reruns these for any localisation change. They only read English yml.
CI_BROAD_SCOPE_EXCEPTIONS = {
    ("decisions", "localisation"),
    ("focus_tree", "localisation"),
}

EXPECTED_STANDALONE_JOBS = {
    "file_paths": ("validate-paths", "validate_file_paths.py", ("map/provinces.bmp",)),
    "mod_descriptors": ("mod-tests", "validate_mod_descriptors.py", ("descriptor.mod",)),
    "style": ("mod-tests", "validate_style.py", ("common/ideas/__routing_probe.txt",)),
}

# Whole-tree or not-yet-scoped CI validators. Adding them to auto-routing
# scans the domain before file scope and blows the one-file lint budget (#124).
INTENTIONALLY_NOT_AUTO_ROUTED = {
    "achievements",
    "ai_path_rules",
    "bonus_names",
    "common_mistakes",
    "cosmetic_tags",
    "country_names",
    "dynamic_modifier_guards",
    "equipment_upkeep",
    "equipment_variants",
    "influence_calls",
    "math_expressions",
    "mio_icons",
    "party_loc",
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


def _load_module(mod_root: Path, relative: str, module_name: str):
    path = mod_root / relative
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ci_workflow(mod_root: Path):
    path = mod_root / ".github" / "workflows" / "test-suite.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_ci_routes(mod_root: Path):
    batches = _load_module(
        mod_root, "tools/validation/validator_batches.py", "md_upstream_validator_batches"
    )
    groups = _load_module(
        mod_root, "tools/validation/change_groups.py", "md_upstream_change_groups"
    )
    routes = {}
    for spec in batches.ALL_SPECS:
        name = Path(spec.script).stem.removeprefix("validate_")
        assert name not in routes, f"duplicate CI validator: {name}"
        routes[name] = tuple(sorted(spec.groups))
    return routes, groups.GROUP_PATTERNS, _load_ci_workflow(mod_root)["jobs"]


def _load_style_scan_patterns(mod_root: Path) -> tuple[str, ...]:
    path = mod_root / "tools" / "validation" / "validate_style.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "_SCAN_PATTERNS"
            for target in node.targets
        ):
            continue
        patterns = ast.literal_eval(node.value)
        assert isinstance(patterns, list)
        assert all(isinstance(pattern, str) for pattern in patterns)
        return tuple(patterns)
    raise AssertionError(f"{path} no longer defines _SCAN_PATTERNS")


def _sparse_checkout_paths(steps: list) -> tuple[str, ...]:
    checkout = next(step for step in steps if "sparse-checkout" in step.get("with", {}))
    return tuple(
        line.strip()
        for line in checkout["with"]["sparse-checkout"].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _load_workspace_paths(mod_root: Path) -> tuple[str, ...]:
    workflow = _load_ci_workflow(mod_root)
    return tuple(workflow["env"]["WORKSPACE_PATHS"].split())


def _load_prepare_workspace_paths(mod_root: Path) -> tuple[str, ...]:
    workflow = _load_ci_workflow(mod_root)
    return _sparse_checkout_paths(workflow["jobs"]["prepare-workspace"]["steps"])


def _load_validate_paths_checkout(mod_root: Path) -> tuple[str, ...]:
    workflow = _load_ci_workflow(mod_root)
    return _sparse_checkout_paths(workflow["jobs"]["validate-paths"]["steps"])


def _probe_path(pattern: str) -> str:
    return pattern.replace("**", "__routing_probe").replace("*", "routing_probe")


@pytest.mark.integration
def test_upstream_commit_registry_snapshot(real_mod_root):
    registry = _load_registry(real_mod_root)
    routes = {spec.script: frozenset(spec.rules) for spec in registry}
    excludes = {spec.script: spec.exclude.pattern for spec in registry if spec.exclude}
    expected = {name: frozenset(rules) for name, rules in EXPECTED_REGISTRY.items()}
    assert routes == expected
    assert excludes == EXPECTED_REGISTRY_EXCLUDES


@pytest.mark.integration
def test_commit_registry_paths_reach_auto_map(real_mod_root):
    for spec in _load_registry(real_mod_root):
        name = spec.script.removeprefix("validate_")
        if name in INTENTIONALLY_NOT_AUTO_ROUTED:
            continue
        for prefix, extension in spec.rules:
            if not prefix and extension == ".txt":
                probes = [
                    _probe_path(pattern)
                    for pattern in _load_style_scan_patterns(real_mod_root)
                    if pattern.endswith(extension)
                ]
                assert probes, f"{spec.script} has no text scan patterns"
            else:
                probes = [prefix if not extension else f"{prefix}__routing_probe{extension}"]
            for probe in probes:
                assert name in _validators_for_path(probe), f"{name} is not auto-routed for {probe}"


@pytest.mark.integration
def test_upstream_ci_routing_snapshot(real_mod_root):
    routes, filters, jobs = _load_ci_routes(real_mod_root)
    assert routes == EXPECTED_CI_ROUTING
    actual_filters = {name: frozenset(paths) for name, paths in filters.items()}
    expected_filters = {name: frozenset(paths) for name, paths in EXPECTED_CI_FILTERS.items()}
    assert actual_filters == expected_filters
    run_steps = [step.get("run", "") for step in jobs["mod-tests"]["steps"]]
    assert any("run_validator_batch.py" in command for command in run_steps)


@pytest.mark.integration
def test_upstream_workspace_paths_snapshot(real_mod_root):
    assert _load_workspace_paths(real_mod_root) == EXPECTED_WORKSPACE_PATHS
    assert _load_prepare_workspace_paths(real_mod_root) == EXPECTED_PREPARE_WORKSPACE_PATHS
    assert _load_validate_paths_checkout(real_mod_root) == EXPECTED_VALIDATE_PATHS_CHECKOUT


@pytest.mark.integration
def test_precise_ci_paths_reach_auto_map(real_mod_root):
    routes, filters, _jobs = _load_ci_routes(real_mod_root)
    missing = set(routes) - set(SCAN_PREFIXES) - INTENTIONALLY_NOT_AUTO_ROUTED
    assert not missing, f"CI validators missing from auto routing: {sorted(missing)}"

    for validator, groups in routes.items():
        if validator in INTENTIONALLY_NOT_AUTO_ROUTED:
            continue
        for group in groups:
            if (validator, group) in COARSE_CI_ROUTES:
                continue
            if (validator, group) in CI_BROAD_SCOPE_EXCEPTIONS:
                for pattern in filters[group]:
                    probe = _probe_path(pattern)
                    assert validator not in _validators_for_path(
                        probe
                    ), f"{validator} unexpectedly routes broader CI pattern {pattern}"
                assert validator in _validators_for_path("localisation/english/__routing_probe.yml")
                assert validator not in _validators_for_path(
                    "localisation/french/__routing_probe.yml"
                )
                continue
            for pattern in filters[group]:
                probe = _probe_path(pattern)
                assert validator in _validators_for_path(
                    probe
                ), f"{validator} is not auto-routed for CI pattern {pattern}"


@pytest.mark.integration
@pytest.mark.parametrize(
    "validator,job_name,script,probes",
    [
        (validator, job_name, script, probes)
        for validator, (job_name, script, probes) in EXPECTED_STANDALONE_JOBS.items()
    ],
)
def test_standalone_ci_validator_remains_wired(real_mod_root, validator, job_name, script, probes):
    _routes, _filters, jobs = _load_ci_routes(real_mod_root)
    assert job_name in jobs
    run_steps = [step["run"] for step in jobs[job_name]["steps"] if "run" in step]
    assert any(
        script in command for command in run_steps
    ), f"{validator} is no longer run by {job_name}"
    for probe in probes:
        assert validator in _validators_for_path(probe)
