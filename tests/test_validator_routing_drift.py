"""Integration contract between lint auto-routing and upstream CI routing."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
import yaml

from md_mcp.tools.lint_validators import (
    AUTO_ROUTING_EXCLUDED,
    _upstream_args,
    _upstream_routing,
    _validators_for_path,
    select_validators,
)
from md_mcp.validators import SLOW_VALIDATORS, available_validators

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
    "validate_ai_roles": (("common/ai_strategy/", ".txt"), ("common/ai_templates/", ".txt")),
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
INTENTIONALLY_NOT_AUTO_ROUTED = {
    "achievements",
    "ai_path_rules",
    "bonus_names",
    "common_mistakes",
    "cosmetic_tags",
    "country_names",
    "dynamic_modifier_guards",
    "equipment_upkeep",
    "influence_calls",
    "math_expressions",
    "mio_icons",
    "party_loc",
    "set_variables",
    "unused_scripted",
    "variables",
} | SLOW_VALIDATORS


def _load_module(mod_root: Path, relative: str, name: str):
    path = mod_root / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_registry(mod_root: Path):
    module = _load_module(mod_root, "tools/precommit_validate.py", "md_upstream_precommit")
    return module._REGISTRY


def _load_style_scan_patterns(mod_root: Path) -> tuple[str, ...]:
    path = mod_root / "tools/validation/validate_style.py"
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


def _load_workflow(mod_root: Path):
    return yaml.safe_load(
        (mod_root / ".github/workflows/test-suite.yml").read_text(encoding="utf-8")
    )


def _probe(pattern: str) -> str:
    return pattern.replace("**", "__routing_probe").replace("*", "routing_probe")


def _sparse_paths(steps: list) -> set[str]:
    step = next(step for step in steps if "sparse-checkout" in step.get("with", {}))
    return {
        line.strip()
        for line in step["with"]["sparse-checkout"].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


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
                    _probe(pattern)
                    for pattern in _load_style_scan_patterns(real_mod_root)
                    if pattern.endswith(extension)
                ]
                assert probes, f"{spec.script} has no text scan patterns"
            else:
                probes = [prefix if not extension else f"{prefix}__routing_probe{extension}"]
            for probe in probes:
                assert name in _validators_for_path(probe), f"{name} is not auto-routed for {probe}"


@pytest.mark.integration
def test_auto_routing_matches_current_upstream_groups(real_mod_root):
    batches, change_groups = _upstream_routing(str(real_mod_root.resolve()))
    available = {info.name for info in available_validators(real_mod_root)}
    excluded = {
        Path(script).stem.removeprefix("validate_").replace("-", "_")
        for script in batches._IMPACT_EXCLUDED_SCRIPTS
    }

    # Exercise representative files from every current CI group. The expected
    # set is computed from upstream's live specs and classifier, not a copied
    # list that can drift independently.
    covered_groups = set()
    for spec in batches.ALL_SPECS:
        for group in spec.groups:
            patterns = change_groups.GROUP_PATTERNS[group]
            probe = _probe(patterns[0])
            changed = change_groups.classify([probe])
            assert changed[group] is True, f"upstream does not classify {probe} as {group}"
            selected = {
                Path(candidate.script).stem.removeprefix("validate_").replace("-", "_")
                for candidate in batches.ALL_SPECS
                if set(candidate.groups).intersection(
                    name for name, value in changed.items() if value is True
                )
            }
            expected = (selected - SLOW_VALIDATORS - excluded) & available
            expected -= AUTO_ROUTING_EXCLUDED
            if changed.get("file-paths"):
                expected.add("file_paths")
            if changed.get("style"):
                expected.add("style")
            expected &= available
            actual = set(select_validators([probe], available, mod_root=real_mod_root))
            assert actual == expected, f"auto route drift for {probe}: {actual ^ expected}"
            covered_groups.add(group)

    assert set().union(*(set(s.groups) for s in batches.ALL_SPECS)) <= covered_groups


@pytest.mark.integration
def test_impact_exclusions_and_validator_arguments_follow_upstream(real_mod_root):
    batches, _groups = _upstream_routing(str(real_mod_root.resolve()))
    available = {info.name for info in available_validators(real_mod_root)}
    excluded_names = {
        Path(script).stem.removeprefix("validate_").replace("-", "_")
        for script in batches._IMPACT_EXCLUDED_SCRIPTS
    }
    for script in batches._IMPACT_EXCLUDED_SCRIPTS:
        assert (
            select_validators([f"tools/validation/{script}"], available, mod_root=real_mod_root)
            == []
        )
    run_all = set(select_validators(None, available, mod_root=real_mod_root))
    assert not (excluded_names & available & run_all)

    expected_args = {
        Path(spec.script).stem.removeprefix("validate_").replace("-", "_"): tuple(spec.args)
        for spec in (*batches.ALL_SPECS, *batches.IMPACT_ONLY_SPECS)
        if spec.args
    }
    assert _upstream_args(real_mod_root) == expected_args
    assert expected_args["variables"] == ("--redundant-focus-flags",)
    assert expected_args["math_expressions"] == ("--clamp-bounds",)
    assert expected_args["decisions"] == ("--unannounced-categories",)


@pytest.mark.integration
@pytest.mark.parametrize(
    "path",
    [
        "tools/validation/validate_decisions.py",
        "tools/shared_utils.py",
        "tools/report_lib/foo.py",
    ],
)
def test_tooling_auto_selection_matches_upstream_impact_selector(real_mod_root, path):
    batches, _groups = _upstream_routing(str(real_mod_root.resolve()))
    available = {info.name for info in available_validators(real_mod_root)}
    selected, adhoc = batches.select_for_changed_files([path])
    expected = {
        Path(spec.script).stem.removeprefix("validate_").replace("-", "_")
        for spec in [*selected, *adhoc]
    }
    excluded = {
        Path(script).stem.removeprefix("validate_").replace("-", "_")
        for script in batches._IMPACT_EXCLUDED_SCRIPTS
    }
    expected -= SLOW_VALIDATORS | AUTO_ROUTING_EXCLUDED | excluded
    expected &= available

    actual = set(select_validators([path], available, mod_root=real_mod_root))

    assert actual == expected
    if path == "tools/shared_utils.py":
        assert {"style", "mod_descriptors"} <= actual
    if path == "tools/report_lib/foo.py":
        assert {"variables", "decisions"} <= actual


@pytest.mark.integration
def test_ci_workspace_contains_current_routing_inputs(real_mod_root):
    workflow = _load_workflow(real_mod_root)
    workspace = set(workflow["env"]["WORKSPACE_PATHS"].split())
    profile = set(
        line.strip().strip("/")
        for line in (real_mod_root / "tools/validation/ci_workspace_profile.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("!")
    )
    checkout = _sparse_paths(workflow["jobs"]["validate-paths"]["steps"])

    # These are current contract inputs used by the live routing/workspace
    # definitions. In particular graphic-db was added after the old snapshot.
    required = {"validation_config.json", "gfx/interface/equipmentdesigner/graphic_db"}
    assert required <= workspace
    assert required <= profile
    assert "tools" in checkout

    group_module = _load_module(
        real_mod_root, "tools/validation/change_groups.py", "md_test_change_groups"
    )
    assert (
        "gfx/interface/equipmentdesigner/graphic_db/**" in group_module.GROUP_PATTERNS["graphic-db"]
    )


@pytest.mark.integration
def test_upstream_standalone_validators_remain_wired(real_mod_root):
    jobs = _load_workflow(real_mod_root)["jobs"]
    required = [
        ("validate-paths", "validate_file_paths.py"),
        ("mod-tests", "validate_mod_descriptors.py"),
        ("mod-tests", "validate_style.py"),
    ]
    for job_name, script in required:
        commands = [step["run"] for step in jobs[job_name]["steps"] if "run" in step]
        assert any(script in command for command in commands)
    assert "style" in _validators_for_path("common/ideas/__routing_probe.txt")


@pytest.mark.integration
def test_upstream_style_scan_patterns_remain_text_scoped(real_mod_root):
    path = real_mod_root / "tools/validation/validate_style.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    patterns = next(
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_SCAN_PATTERNS"
            for target in node.targets
        )
    )
    assert patterns
    assert all(pattern.endswith(".txt") for pattern in patterns)
