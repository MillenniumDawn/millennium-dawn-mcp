"""Tests for the in-memory upstream standardization wrapper."""

from __future__ import annotations

import json
import sys
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


def test_path_resolves_overlay_first_then_base(tmp_path: Path, monkeypatch):
    base = tmp_path / "base"
    submod = tmp_path / "submod"
    shadowed = "events/shadowed.txt"
    for root, rel, text in (
        (base, shadowed, "base\n"),
        (submod, shadowed, "overlay\n"),
        (base, "events/base_only.txt", "base only\n"),
        (submod, "events/overlay_only.txt", "overlay only\n"),
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    monkeypatch.setattr(
        standardize_tools,
        "_load_standardize_api",
        lambda _root: SimpleNamespace(
            kind_for_path=lambda path: "event" if path.startswith("events/") else None,
            standardize_text=lambda _kind, text, _root: text.upper(),
        ),
    )

    def run(rel: str) -> dict:
        return standardize_tool(base, submod, path=rel)

    assert run(shadowed)["txt"] == "OVERLAY\n"
    assert run("events/overlay_only.txt")["txt"] == "OVERLAY ONLY\n"
    assert run("events/base_only.txt")["txt"] == "BASE ONLY\n"


# ---------------------------------------------------------------------------
# Unit-level coverage with a synthetic upstream API — the real one lives in a
# Millennium-Dawn checkout, so the loader and its error branches only run in
# the integration suite otherwise.
# ---------------------------------------------------------------------------

_FAKE_API_SOURCE = """
MARKER = "first"


def kind_for_path(path):
    return {
        "common/national_focus/test.txt": "focus",
        "events/test.txt": "event",
        "misc/test.txt": None,
    }.get(path)


def standardize_text(kind, text, mod_root):
    return text.upper() if kind == "focus" else None
"""

_FAKE_API_SOURCE_V2 = _FAKE_API_SOURCE.replace('MARKER = "first"', 'MARKER = "second"')

_FAKE_API_BAD_KIND_SOURCE = """
def kind_for_path(path):
    return "localisation"


def standardize_text(kind, text, mod_root):
    return None
"""

_FAKE_API_RAISING_SOURCE = """
def kind_for_path(path):
    return "focus"


def standardize_text(kind, text, mod_root):
    raise ValueError("upstream exploded")
"""


@pytest.fixture(autouse=True)
def _reset_upstream_loader_state():
    saved_path = sys.path.copy()
    saved_modules = {name: sys.modules.get(name) for name in standardize_tools._UPSTREAM_MODULES}
    yield
    sys.path[:] = saved_path
    for name, module in saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    standardize_tools._loaded_mod_root = None
    standardize_tools._loaded_api = None
    standardize_tools._inserted_dirs = []


def _plant_api(root: Path, source: str = _FAKE_API_SOURCE) -> Path:
    api_dir = root / "tools" / "standardization"
    api_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / "standardize_api.py").write_text(source, encoding="utf-8")
    return api_dir


def test_loader_plants_sys_path_and_caches_per_root(tmp_path: Path):
    root_one = tmp_path / "mod_one"
    root_two = tmp_path / "mod_two"
    _plant_api(root_one, _FAKE_API_SOURCE)
    _plant_api(root_two, _FAKE_API_SOURCE_V2)

    first = standardize_tools._load_standardize_api(root_one)
    assert first.MARKER == "first"
    assert standardize_tools._loaded_api is first
    assert standardize_tools._load_standardize_api(root_one) is first

    second = standardize_tools._load_standardize_api(root_two)
    assert second is not first
    assert second.MARKER == "second"
    assert str(root_one.resolve() / "tools") not in sys.path
    assert str(root_two.resolve() / "tools" / "standardization") in sys.path


def test_content_mode_reports_missing_upstream_api(tmp_path: Path):
    result = standardize_tool(tmp_path, content="event", content_type="event")

    assert result["ok"] is False
    assert "Upstream standardization API not found" in result["error"]
    assert "tools/standardization" in result["error"]


def test_path_mode_reports_missing_upstream_api(tmp_path: Path):
    source = tmp_path / "common" / "national_focus" / "test.txt"
    source.parent.mkdir(parents=True)
    source.write_text("focus_tree = { }\n", encoding="utf-8")

    result = standardize_tool(tmp_path, path="common/national_focus/test.txt")

    assert result["ok"] is False
    assert "Upstream standardization API not found" in result["error"]


def test_loader_dedupes_preexisting_sys_path_entries(tmp_path: Path):
    root = tmp_path / "mod"
    _plant_api(root)
    tools_dir = str(root.resolve() / "tools")
    sys.path.insert(0, tools_dir)
    standardize_tools._loaded_mod_root = None
    standardize_tools._loaded_api = None
    standardize_tools._inserted_dirs = []

    api = standardize_tools._load_standardize_api(root)

    assert api.MARKER == "first"
    assert sys.path.count(tools_dir) == 1


def test_path_mode_reports_path_validation_errors(tmp_path: Path):
    escape = standardize_tool(tmp_path, path="../evil.txt")
    assert escape["ok"] is False
    assert "outside" in escape["error"]

    missing = standardize_tool(tmp_path, path="events/missing.txt")
    assert missing["ok"] is False
    assert "not a regular file" in missing["error"]


def test_path_mode_reports_undetected_kind(tmp_path: Path):
    source = tmp_path / "misc" / "test.txt"
    source.parent.mkdir(parents=True)
    source.write_text("unrouted = { }\n", encoding="utf-8")
    _plant_api(tmp_path)

    result = standardize_tool(tmp_path, path="misc/test.txt")

    assert result["ok"] is False
    assert "Could not detect" in result["error"]


def test_path_mode_rejects_kind_outside_supported_set(tmp_path: Path):
    source = tmp_path / "common" / "national_focus" / "test.txt"
    source.parent.mkdir(parents=True)
    source.write_text("focus_tree = { }\n", encoding="utf-8")
    _plant_api(tmp_path, _FAKE_API_BAD_KIND_SOURCE)

    result = standardize_tool(tmp_path, path="common/national_focus/test.txt")

    assert result["ok"] is False
    assert "Could not detect a standardizer kind" in result["error"]


def test_path_mode_reports_invalid_utf8(tmp_path: Path):
    source = tmp_path / "common" / "national_focus" / "test.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\xff\xfe focus_tree")
    _plant_api(tmp_path)

    result = standardize_tool(tmp_path, path="common/national_focus/test.txt")

    assert result["ok"] is False
    assert "Invalid UTF-8" in result["error"]


def test_path_mode_reports_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "common" / "national_focus" / "test.txt"
    source.parent.mkdir(parents=True)
    source.write_text("focus_tree = { }\n", encoding="utf-8")
    _plant_api(tmp_path)

    def unreadable(path: Path) -> bytes:
        assert path == source
        raise PermissionError("Permission denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    result = standardize_tool(tmp_path, path="common/national_focus/test.txt")

    assert result["ok"] is False
    assert "Permission denied" in result["error"]


def test_standardize_text_exception_surfaces(tmp_path: Path):
    _plant_api(tmp_path, _FAKE_API_RAISING_SOURCE)

    result = standardize_tool(
        tmp_path,
        content="focus_tree = { }\n",
        content_type="focus",
    )

    assert result["ok"] is False
    assert result["kind"] == "focus"
    assert "upstream exploded" in result["error"]


def test_clip_txt_binary_search_trims_txt_to_fit():
    junk = "y" * 99_000
    out = standardize_tools._clip_txt(
        {"ok": True, "kind": "focus", "changed": False, "junk": junk},
        "x" * 2_000,
    )

    assert out["txt_truncated"] is True
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
    assert len(out["txt"]) < 2_000


def test_clip_txt_returns_bounded_fallback_when_nothing_fits():
    junk = "y" * 99_900
    out = standardize_tools._clip_txt(
        {"ok": True, "kind": "focus", "changed": False, "junk": junk},
        "x" * 2_000,
    )

    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
    assert "exceeded byte budget" in out["error"]
