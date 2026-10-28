"""Parity checks for in-memory adapters over the upstream transforms."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from md_mcp.tools import content_transforms as transforms


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("strip_dynmod_tag_gates", "modifier = {\n enable = { always = yes }\n}\n"),
        (
            "add_idea_equipment_bonus_instant",
            "equipment_bonus = { infantry_equipment = { attack = 0.1 } }\n",
        ),
    ],
)
def test_line_transforms_match_upstream_core(real_mod_root: Path, name: str, text: str):
    module = transforms._load(real_mod_root, name)
    if name == "strip_dynmod_tag_gates":
        expected = "\n".join(module.strip_enable_gates(text.split("\n"))[0])
    else:
        expected = module.add_instant(text)[0]
    result = transforms.transform_tool(real_mod_root, content=text, transform=name)
    assert result["ok"] is True
    assert result["txt"] == expected
    assert result["changed"] is (expected != text)


@pytest.mark.integration
def test_ai_path_mapping_parity(real_mod_root: Path):
    text = "focus_tree = {\n focus = {\n  id = TST_a\n }\n}\n"
    mapping = "TST_a -\n"
    upstream = transforms._load(real_mod_root, "apply_ai_path_weights")
    expected = "".join(
        upstream.apply(text.splitlines(keepends=True), upstream.parse_mapping(mapping), "TST")[0]
    )
    result = transforms.transform_tool(
        real_mod_root,
        content=text,
        transform="apply_ai_path_weights",
        mapping=mapping,
        tag="TST",
    )
    assert result["txt"] == expected
    assert result["changed"] is True


@pytest.mark.integration
def test_idea_gate_and_logging_transforms_are_upstream_backed(real_mod_root: Path):
    idea_text = (
        "ideas = {\n country = {\n  TST_example = {\n   allowed = { always = yes }\n  }\n }\n}\n"
    )
    gates = transforms._load(real_mod_root, "strip_idea_allowed_gates")
    shared = transforms._load(real_mod_root, "shared_utils")
    expected_lines = gates.strip_allowed_blocks(
        idea_text.split("\n"), shared.get_slotless_idea_categories(str(real_mod_root))
    )[0]
    expected = "\n".join(expected_lines)
    gate_result = transforms.transform_tool(
        real_mod_root, content=idea_text, transform="strip_idea_allowed_gates"
    )
    assert gate_result["txt"] == expected

    focus_text = (
        "focus_tree = {\n focus = {\n id = TST_root\n completion_reward = {\n"
        "  add_political_power = 1\n }\n }\n}\n"
        "# content above exercises a logging target in this fixture\n"
    )
    logging = transforms._load(real_mod_root, "logging_tool")
    lines = focus_text.splitlines(keepends=True)
    targets = list(
        logging._find_effect_targets(
            lines,
            entity=lambda key, level: key in ("focus", "shared_focus") and level >= 0,
            effect_keys={"completion_reward"},
        )
    )
    assert len(targets) == 1
    entity_index, effect_index, key = targets[0]
    log_text = logging._focus_log_text(lines, entity_index, effect_index, key)
    expected_logging = "".join(
        logging._logged_opener(line, log_text) if index == effect_index else line
        for index, line in enumerate(lines)
    )
    log_result = transforms.transform_tool(
        real_mod_root, content=focus_text, transform="logging_tool", content_type="focus"
    )
    assert log_result["txt"] == expected_logging
    assert log_result["changed"] is True


@pytest.mark.integration
def test_unchanged_transform_reports_false(real_mod_root: Path):
    content = "modifier = { enable = { has_idea = example } }\n"
    result = transforms.transform_tool(
        real_mod_root, content=content, transform="strip_dynmod_tag_gates"
    )
    assert result["changed"] is False
    assert result["txt"] == content


def test_unknown_transform_is_rejected(tmp_path: Path):
    result = transforms.transform_tool(tmp_path, content="same", transform="unknown")
    assert result == {"ok": False, "error": "Unsupported transform: unknown"}


def test_transform_output_budget_and_unchanged_false(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        transforms,
        "_load",
        lambda _root, _name: SimpleNamespace(strip_enable_gates=lambda lines: (lines, 0, 0, 0)),
    )
    unchanged = transforms.transform_tool(
        tmp_path, content="modifier = { }\n", transform="strip_dynmod_tag_gates"
    )
    assert unchanged["changed"] is False
    assert unchanged["summary"]["removed"] == 0

    monkeypatch.setattr(
        transforms,
        "_load",
        lambda _root, _name: SimpleNamespace(
            strip_enable_gates=lambda _lines: (["x" * 120_000], 1, 0, 0)
        ),
    )
    clipped = transforms.transform_tool(tmp_path, content="x", transform="strip_dynmod_tag_gates")
    assert clipped["txt_truncated"] is True
    assert len(clipped["txt"]) < 120_000
