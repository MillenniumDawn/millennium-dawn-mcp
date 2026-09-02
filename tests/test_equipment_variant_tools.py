"""Tests for the interactive equipment-variant compatibility tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from md_mcp.tools.equipment_variant_tools import (
    EquipmentVariantChecker,
    check_equipment_variant_tool,
)
from md_mcp.util.response import BUDGET_BYTES

_HELPER = """
from dataclasses import dataclass

builds = 0

@dataclass
class Finding:
    line: int
    kind: str
    message: str
    hull: str


def build_equipment_index(units_dir):
    global builds
    builds += 1
    return object()


def check_created_variants(content, index):
    if "oversized" in content:
        return [Finding(i, "unknown_slot", "x" * 200, "test_hull") for i in range(1_000)]
    if "bad_slot" in content:
        return [Finding(5, "unknown_slot", "bad_slot is not available", "test_hull")]
    return []
"""

_VALID = """create_equipment_variant = {
    type = test_hull
    modules = {
        valid_slot = valid_module
    }
}"""


def _install_helper(mod_root: Path) -> None:
    validation = mod_root / "tools" / "validation"
    validation.mkdir(parents=True, exist_ok=True)
    (validation / "equipment_module_slots.py").write_text(_HELPER, encoding="utf-8")
    equipment = mod_root / "common" / "units" / "equipment"
    equipment.mkdir(parents=True, exist_ok=True)
    (equipment / "hulls.txt").write_text("test_hull = {}\n", encoding="utf-8")


def test_check_equipment_variant_accepts_valid_variant(fake_mod_root):
    _install_helper(fake_mod_root)

    result = check_equipment_variant_tool(EquipmentVariantChecker(fake_mod_root), _VALID)

    assert result == {
        "ok": True,
        "valid": True,
        "issues_total": 0,
        "returned": 0,
        "truncated": False,
        "issues": [],
    }


def test_check_equipment_variant_returns_structured_compatibility_issue(fake_mod_root):
    _install_helper(fake_mod_root)
    text = _VALID.replace("valid_slot", "bad_slot")

    result = check_equipment_variant_tool(EquipmentVariantChecker(fake_mod_root), text)

    assert result["ok"] is True
    assert result["valid"] is False
    assert result["issues_total"] == 1
    assert result["issues"] == [
        {
            "line": 5,
            "severity": "error",
            "kind": "unknown_slot",
            "message": "bad_slot is not available",
            "hull": "test_hull",
        }
    ]


def test_check_equipment_variant_rejects_invalid_or_ambiguous_input(fake_mod_root):
    _install_helper(fake_mod_root)
    checker = EquipmentVariantChecker(fake_mod_root)

    malformed = check_equipment_variant_tool(checker, "create_equipment_variant = {{{")
    missing = check_equipment_variant_tool(checker, "focus = { id = TST_focus }")
    multiple = check_equipment_variant_tool(checker, f"{_VALID}\n{_VALID}")
    wrong_operator = check_equipment_variant_tool(checker, _VALID.replace("type =", "type !="))
    wrong_modules_operator = check_equipment_variant_tool(
        checker, _VALID.replace("modules =", "modules !=")
    )

    assert malformed["ok"] is False
    assert malformed["error"].startswith("Invalid paradox script:")
    assert missing == {
        "ok": False,
        "error": "Input must contain one create_equipment_variant = { ... } block.",
    }
    assert multiple == {
        "ok": False,
        "error": "Input must contain exactly one create_equipment_variant block.",
        "variants_found": 2,
    }
    assert wrong_operator == {
        "ok": False,
        "error": "create_equipment_variant requires a scalar type.",
    }
    assert wrong_modules_operator == {
        "ok": False,
        "error": "create_equipment_variant requires a modules block.",
    }


@pytest.mark.integration
def test_check_equipment_variant_uses_current_mod_rules(real_mod_root):
    # Mirrors Zulfiqar in history/countries/PER - Iran.txt.
    valid = """create_equipment_variant = {
    name = "Zulfiqar"
    type = medium_tank_chassis_1
    parent_version = 0
    modules = {
        main_armament_slot = tank_medium_cannon_2
        ammunition_load_slot = mixed_main_ammo_2
        turret_type_slot = tank_base_tank_turret
        suspension_type_slot = tank_torsion_bar_suspension_medium
        armor_type_slot = tank_composite_armor_gen1
        engine_type_slot = tank_diesel_engine_gen3
        reload_type_slot = automatic_loading
        special_type_slot_1 = smoke_launchers
        special_type_slot_2 = empty
        special_type_slot_4 = tank_battlestation_2
        special_type_slot_6 = reactive_armor_gen1
    }
    upgrades = {
        tank_nsb_armor_upgrade = 3
    }
    icon = "gfx/interface/technologies/PER/LAND/Zulfiqar.dds"
}"""

    clean = check_equipment_variant_tool(EquipmentVariantChecker(real_mod_root), valid)
    invalid = check_equipment_variant_tool(
        EquipmentVariantChecker(real_mod_root),
        valid.replace(
            "    modules = {\n",
            "    modules = {\n        missing_slot = tank_diesel_engine_gen3\n",
        ),
    )

    assert clean["ok"] is True
    assert clean["valid"] is True
    assert clean["issues"] == []
    assert invalid["ok"] is True
    assert invalid["valid"] is False
    assert invalid["issues_total"] == 1
    assert invalid["issues"] == [
        {
            "line": 6,
            "severity": "error",
            "kind": "unknown_slot",
            "message": (
                "hull 'medium_tank_chassis_1' has no slot 'missing_slot' — "
                "module assignment is silently ignored"
            ),
            "hull": "medium_tank_chassis_1",
        }
    ]


def test_check_equipment_variant_paginates_and_refreshes_cached_sources(fake_mod_root):
    _install_helper(fake_mod_root)
    checker = EquipmentVariantChecker(fake_mod_root)
    text = _VALID.replace("valid_slot", "bad_slot")

    first = check_equipment_variant_tool(checker, text, limit=0)
    second = check_equipment_variant_tool(checker, text, limit=1)
    equipment = fake_mod_root / "common" / "units" / "equipment" / "hulls.txt"
    equipment.write_text("test_hull = {}\nsecond_hull = {}\n", encoding="utf-8")
    third = check_equipment_variant_tool(checker, text)

    assert first["returned"] == 0
    assert first["truncated"] is True
    assert second["returned"] == 1
    assert checker._module is not None
    assert checker._module.builds == 2
    assert third["ok"] is True

    original_module = checker._module
    helper = fake_mod_root / "tools" / "validation" / "equipment_module_slots.py"
    helper.write_text(f"{_HELPER}\n", encoding="utf-8")
    fourth = check_equipment_variant_tool(checker, text)

    assert checker._module is not original_module
    assert checker._module.builds == 1
    assert fourth["ok"] is True


def test_check_equipment_variant_budget_guard_drops_oversized_issues(fake_mod_root):
    _install_helper(fake_mod_root)
    text = _VALID.replace("valid_slot", "oversized")

    result = check_equipment_variant_tool(EquipmentVariantChecker(fake_mod_root), text, limit=1_000)

    assert result["ok"] is True
    assert result["issues_total"] == 1_000
    assert result["returned"] == 1_000
    assert result["size_truncated"] is True
    assert "issues" not in result
    assert len(json.dumps(result).encode("utf-8")) <= BUDGET_BYTES
