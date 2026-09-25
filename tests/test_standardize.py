"""Tests for the in-memory upstream standardization wrapper."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from md_mcp.tools import standardize_tools
from md_mcp.tools.standardize_tools import standardize_tool
from md_mcp.util.response import BUDGET_BYTES

CASES = [
    (
        "focus",
        (
            "focus_tree = { id = TST focus = { id = TST_root x = 0 y = 0 "
            "cost = 1 completion_reward = { add_political_power = 1 } } }\n"
        ),
    ),
    (
        "event",
        (
            "country_event = { id = TST.1 title = TST.1.t desc = TST.1.d "
            "is_triggered_only = yes option = { name = TST.1.a "
            "add_political_power = 1 } }\n"
        ),
    ),
    (
        "decision",
        (
            "TST_category = { TST_decision = { cost = 10 complete_effect = "
            "{ add_political_power = 1 } } }\n"
        ),
    ),
    (
        "idea",
        (
            "ideas = { country = { TST_idea = { allowed = { original_tag = TST } "
            "modifier = { stability_factor = 0.1 } } } }\n"
        ),
    ),
    ("mio", "TST_mio = { category = infantry_mio }\n"),
    ("technology", "technologies = { land_techs = { tech = { year = 1936 } } }\n"),
    (
        "history",
        "1936.1.1 = {\n\tset_variable = { var = TST value = 1 }\n}\n",
    ),
]


@pytest.mark.parametrize(("kind", "text"), CASES)
def test_upstream_standardizer_round_trips(real_mod_root: Path, kind: str, text: str):
    api = standardize_tools._load_standardize_api(real_mod_root)
    expected = api.standardize_text(kind, text, str(real_mod_root))
    result = standardize_tool(real_mod_root, content=text, content_type=kind)

    assert result["ok"] is True
    assert result["kind"] == kind
    assert result["txt"] == (text if expected is None else expected)
    assert result["changed"] is (result["txt"] != text)
    assert not result["txt"].startswith("\ufeff")

    second_pass = api.standardize_text(kind, result["txt"], str(real_mod_root))
    assert (result["txt"] if second_pass is None else second_pass) == result["txt"]


def test_path_input_detects_kind_and_does_not_write(
    real_mod_root: Path, tmp_path: Path, monkeypatch
):
    api = standardize_tools._load_standardize_api(real_mod_root)
    mod_root = tmp_path / "mod"
    source_path = mod_root / "common" / "national_focus" / "test.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(CASES[0][1], encoding="utf-8")
    original = source_path.read_bytes()
    monkeypatch.setattr(standardize_tools, "_load_standardize_api", lambda _root: api)

    result = standardize_tool(mod_root, path="common/national_focus/test.txt")

    expected = api.standardize_text("focus", original.decode("utf-8"), str(mod_root))
    assert result["ok"] is True
    assert result["kind"] == "focus"
    assert result["txt"] == (original.decode("utf-8") if expected is None else expected)
    assert source_path.read_bytes() == original

    unknown_path = mod_root / "misc" / "test.txt"
    unknown_path.parent.mkdir(parents=True)
    unknown_path.write_text("unrouted = { }\n", encoding="utf-8")
    unknown = standardize_tool(mod_root, path="misc/test.txt")
    assert unknown["ok"] is False
    assert "Could not detect" in unknown["error"]


def test_rejects_missing_or_ambiguous_input_and_localisation(fake_mod_root: Path):
    assert standardize_tool(fake_mod_root)["ok"] is False
    assert standardize_tool(fake_mod_root, content="event")["ok"] is False
    assert standardize_tool(fake_mod_root, content="event", path="events/test.txt")["ok"] is False
    assert (
        standardize_tool(
            fake_mod_root,
            content="key: value",
            content_type="localisation",
        )["ok"]
        is False
    )
    assert (
        "Localisation is not supported"
        in standardize_tool(
            fake_mod_root,
            content="key: value",
            content_type="localisation",
        )["error"]
    )


def test_removes_bom_and_reports_change(fake_mod_root: Path, monkeypatch):
    monkeypatch.setattr(
        standardize_tools,
        "_load_standardize_api",
        lambda _root: SimpleNamespace(standardize_text=lambda _kind, text, _root: text),
    )

    result = standardize_tool(
        fake_mod_root,
        content="\ufeffcountry_event = { }\n",
        content_type="event",
    )

    assert result["ok"] is True
    assert result["txt"] == "country_event = { }\n"
    assert result["changed"] is True
    assert not result["txt"].startswith("\ufeff")


def test_clips_oversized_text_and_warns_not_to_write(fake_mod_root: Path, monkeypatch):
    oversized = "country_event = { }\n" + "x" * 120_000
    monkeypatch.setattr(
        standardize_tools,
        "_load_standardize_api",
        lambda _root: SimpleNamespace(standardize_text=lambda _kind, _text, _root: oversized),
    )

    result = standardize_tool(fake_mod_root, content="event", content_type="event")

    assert result["ok"] is True
    assert result["txt_truncated"] is True
    assert "do NOT write clipped content back" in result["note"]
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
