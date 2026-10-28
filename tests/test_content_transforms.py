"""Parity checks for in-memory adapters over the upstream transforms."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

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
def test_technology_logging_remove_captures_upstream_stdout(real_mod_root: Path):
    content = (
        "technologies = {\n TST_example = {\n  on_research_complete = {\n"
        '   log = "[GetDateText]: [Root.GetName]: add tech TST_example"\n'
        "  }\n }\n}\n# keep this input above logging_tool's 100-byte threshold\n"
    )
    result = transforms.transform_tool(
        real_mod_root,
        content=content,
        transform="logging_tool",
        action="remove",
        content_type="technology",
    )
    assert result["ok"] is True
    assert result["summary"]["logs_changed"] > 0
    assert 'log = "[GetDateText]' not in result["txt"]


@pytest.mark.integration
def test_event_logging_matches_text_mode_newline_and_bom_behavior(real_mod_root: Path):
    content = (
        "\ufeffcountry_event = {\r\n id = TST.1\r\n immediate = {\r\n"
        "  add_political_power = 1\r\n }\r\n}\r\n"
        "# pad above upstream's minimum file size before logging this event\r\n"
    )
    result = transforms.transform_tool(
        real_mod_root,
        content=content,
        transform="logging_tool",
        action="add",
        content_type="event",
    )
    assert result["ok"] is True
    assert result["changed"] is True
    assert "\r" not in result["txt"]
    assert not result["txt"].startswith("\ufeff")
    assert 'log = "[GetDateText]' in result["txt"]


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


def test_transform_upstream_error_is_bounded(tmp_path: Path, monkeypatch):
    def fail(_root, _name):
        raise ValueError("bad mapping")

    monkeypatch.setattr(transforms, "_load", fail)
    result = transforms.transform_tool(
        tmp_path, content="text", transform="add_idea_equipment_bonus_instant"
    )
    assert result == {
        "ok": False,
        "error": "bad mapping",
        "transform": "add_idea_equipment_bonus_instant",
    }


@pytest.mark.parametrize(
    ("stdout", "returncode", "error"),
    [
        ("not-json", 0, "invalid output"),
        (json.dumps({"ok": False, "error": "bad input"}), 1, "bad input"),
    ],
)
def test_logging_child_errors_surface(
    tmp_path: Path, monkeypatch, stdout: str, returncode: int, error: str
):
    result = subprocess.CompletedProcess([], returncode, stdout, "diagnostic")
    monkeypatch.setattr(transforms, "run_in_group", lambda *_args, **_kwargs: result)
    with pytest.raises(RuntimeError, match=error):
        transforms._logging_transform(tmp_path, "text", "add", "focus")


def test_logging_child_protocol_success_and_upstream_stdout_capture(monkeypatch):
    request = json.dumps(
        {"mod_root": "/unused", "content": "source", "action": "add", "content_type": "focus"}
    )
    stdout = io.StringIO()
    monkeypatch.setattr(transforms.sys, "stdin", io.StringIO(request))
    monkeypatch.setattr(transforms.sys, "stdout", stdout)
    monkeypatch.setattr(
        transforms,
        "_logging_transform_virtual",
        lambda *_args: ("updated", 1),
    )
    assert transforms._logging_child_main() == 0
    assert json.loads(stdout.getvalue()) == {"ok": True, "txt": "updated", "count": 1}
    stdout.seek(0)
    stdout.truncate()
    monkeypatch.setattr(transforms.sys, "stdin", io.StringIO(request))

    def fail(*_args):
        raise ValueError("bad content")

    monkeypatch.setattr(transforms, "_logging_transform_virtual", fail)
    assert transforms._logging_child_main() == 1
    assert json.loads(stdout.getvalue()) == {
        "ok": False,
        "error": "ValueError: bad content",
    }


def test_logging_parent_protocol_success(tmp_path: Path, monkeypatch):
    response = {"ok": True, "txt": "updated", "count": 2}
    result = subprocess.CompletedProcess([], 0, json.dumps(response), "")
    monkeypatch.setattr(transforms, "run_in_group", lambda *_args, **_kwargs: result)
    assert transforms._logging_transform(tmp_path, "input", "remove", "event") == (
        "updated",
        2,
    )


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


def test_ai_path_transform_calls_upstream_helpers(tmp_path: Path, monkeypatch):
    upstream = SimpleNamespace(
        parse_mapping=lambda value: SimpleNamespace(assignments={"TST_a": "-", "TST_b": "-"}),
        apply=lambda lines, parsed, tag: (["updated\n"], [f"{tag}: applied"]),
    )
    monkeypatch.setattr(transforms, "_load", lambda _root, _name: upstream)
    result = transforms.transform_tool(
        tmp_path,
        content="source\n",
        transform="apply_ai_path_weights",
        mapping="TST_a -",
        tag="TST",
    )
    assert result["txt"] == "updated\n"
    assert result["summary"] == {"notes": ["TST: applied"], "updated_focuses": 2}


def test_idea_gate_transform_uses_upstream_category_helper(tmp_path: Path, monkeypatch):
    upstream = SimpleNamespace(
        strip_allowed_blocks=lambda lines, categories: (
            [*lines, next(iter(categories))],
            1,
            0,
        )
    )
    shared = SimpleNamespace(get_slotless_idea_categories=lambda _root: frozenset({"country"}))
    monkeypatch.setattr(
        transforms,
        "_load",
        lambda _root, name: upstream if name == "strip_idea_allowed_gates" else shared,
    )
    result = transforms.transform_tool(
        tmp_path, content="ideas = {\n", transform="strip_idea_allowed_gates"
    )
    assert result["txt"] == "ideas = {\n\ncountry"
    assert result["summary"] == {"removed": 1, "unbalanced_skipped": 0}


def test_equipment_transform_uses_upstream_helper(tmp_path: Path, monkeypatch):
    upstream = SimpleNamespace(add_instant=lambda content: (content + "instant = yes\n", 1))
    monkeypatch.setattr(transforms, "_load", lambda _root, _name: upstream)
    result = transforms.transform_tool(
        tmp_path, content="equipment_bonus = { }\n", transform="add_idea_equipment_bonus_instant"
    )
    assert result["changed"] is True
    assert result["summary"] == {"added": 1}


def test_logging_transform_dispatches_arguments(tmp_path: Path, monkeypatch):
    calls = []

    def apply(root, content, action, content_type):
        calls.append((root, content, action, content_type))
        return "logged", 3

    monkeypatch.setattr(transforms, "_logging_transform", apply)
    result = transforms.transform_tool(
        tmp_path,
        content="source",
        transform="logging_tool",
        action="remove",
        content_type="decision",
    )
    assert calls == [(tmp_path, "source", "remove", "decision")]
    assert result["summary"] == {
        "action": "remove",
        "content_type": "decision",
        "logs_changed": 3,
    }


def test_logging_virtual_io_never_writes_files(tmp_path: Path, monkeypatch):
    upstream = ModuleType("logging_tool_fixture")

    def add(_root):
        ((filename, path),) = list(
            upstream._iter_directory_paths(_root, "common", "national_focus")
        )
        lines = upstream._read_lines_or_warn(path, filename)
        with upstream._open_output_or_raise(path, filename) as output:
            output.writelines(lines)
        return 1

    def remove_focus(_root):
        ((filename, path),) = list(
            upstream._iter_directory_paths(_root, "common", "national_focus")
        )
        lines = upstream._read_lines_if_large_enough(path)
        with upstream._open_output(path) as output:
            output.writelines(lines)
        return 1

    def remove_idea(_root):
        ((filename, path),) = list(upstream._iter_directory_paths(_root, "common", "ideas"))
        with upstream.open(path, "r", encoding="utf-8") as source:
            lines = source.readlines()
        with upstream._open_output_or_raise(path, filename) as output:
            output.writelines(lines)
        return 1

    fake_module = cast(Any, upstream)
    fake_module.focus_add = add
    fake_module.focus_remove = remove_focus
    for entity in ("event", "idea", "decision", "tech"):
        setattr(upstream, f"{entity}_add", add)
        setattr(upstream, f"{entity}_remove", add)
    fake_module.idea_remove = remove_idea
    monkeypatch.setattr(transforms, "_load", lambda _root, _name: upstream)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("logging transform attempted a filesystem write")

    monkeypatch.setattr(Path, "write_text", forbidden)
    content = "focus = {\n id = TST_a\n}\n#" + ("x" * 120) + "\n"
    output, count = transforms._logging_transform_virtual(tmp_path, content, "add", "focus")
    assert output == content
    assert count == 1
    removed, remove_count = transforms._logging_transform_virtual(
        tmp_path, content, "remove", "focus"
    )
    assert removed == content
    assert remove_count == 1
    idea_removed, idea_count = transforms._logging_transform_virtual(
        tmp_path, content, "remove", "idea"
    )
    assert idea_removed == content
    assert idea_count == 1
    with pytest.raises(ValueError, match="action must"):
        transforms._logging_transform_virtual(tmp_path, content, "bad", "focus")
    with pytest.raises(ValueError, match="content_type must"):
        transforms._logging_transform_virtual(tmp_path, content, "add", "bad")
