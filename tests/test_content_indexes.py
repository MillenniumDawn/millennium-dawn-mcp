from __future__ import annotations

from md_mcp.indexes import OnActionIndex
from md_mcp.indexes.definitions import (
    parse_country_history_file,
    parse_dynamic_modifier_file,
    parse_modifier_definition_file,
    parse_on_action_file,
    parse_technology_file,
    parse_unit_history_file,
)
from md_mcp.tools.resolver_tools import resolve_on_action_content_tool


def test_common_content_indexes_extract_named_blocks(tmp_path):
    cases = [
        (
            "on_actions.txt",
            "on_actions = { tick = { effect = { } } }",
            parse_on_action_file,
            "tick",
            "on_action",
        ),
        (
            "tech.txt",
            "technologies = { radio = { cost = 1 } }",
            parse_technology_file,
            "radio",
            "technology",
        ),
        (
            "dynamic.txt",
            "inflation_modifier = { icon = 1 }",
            parse_dynamic_modifier_file,
            "inflation_modifier",
            "dynamic_modifier",
        ),
        (
            "modifier.txt",
            "stability_factor = { add = 0.1 }",
            parse_modifier_definition_file,
            "stability_factor",
            "modifier_definition",
        ),
    ]
    for filename, content, parser, record_id, kind in cases:
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        result = parser(str(path), f"common/{filename}")
        assert result.records == [
            {"id": record_id, "kind": kind, "file": f"common/{filename}", "line": 1}
        ]


def test_history_indexes_keep_country_and_template_identity(tmp_path):
    country = tmp_path / "TST - Testland.txt"
    country.write_text("capital = 1\n", encoding="utf-8")
    result = parse_country_history_file(str(country), "history/countries/TST - Testland.txt")
    assert result.records is not None
    assert result.records[0]["tag"] == "TST"
    assert result.records[0]["id"] == "TST"

    units = tmp_path / "TST_2000.txt"
    units.write_text(
        'division_template = {\n name = "Infantry"\n}\nunits = { division = { name = "Army" } }',
        encoding="utf-8",
    )
    result = parse_unit_history_file(str(units), "history/units/TST_2000.txt")
    assert result.records is not None
    assert [(record["kind"], record["id"]) for record in result.records] == [
        ("division_template", "TST_2000:Infantry"),
        ("oob", "TST_2000"),
    ]


def test_on_action_resolver_returns_all_additive_hook_sources(tmp_path):
    root = tmp_path / "mod"
    actions = root / "common/on_actions"
    actions.mkdir(parents=True)
    (actions / "a.txt").write_text("on_actions = { on_daily = { } }", encoding="utf-8")
    (actions / "b.txt").write_text("on_actions = { on_daily = { } }", encoding="utf-8")
    index = OnActionIndex(root, tmp_path / "cache", None)

    result = resolve_on_action_content_tool("on_daily", index)

    assert result["ok"] is True
    assert result["sources_total"] == 2
    assert [source["file"] for source in result["sources"]] == [
        "common/on_actions/a.txt",
        "common/on_actions/b.txt",
    ]
