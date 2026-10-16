from __future__ import annotations

import json

import pytest

from md_mcp.config import Settings
from md_mcp.generators import missing_loc
from md_mcp.indexes import EventIndex, FocusIndex, LocalisationIndex


def _tool(fake_mod_root, cache_dir):
    settings = Settings(
        mod_root=fake_mod_root,
        vanilla_path=None,
        cache_dir=cache_dir,
        validator_mode="isolated",
        default_lang="en",
    )
    return lambda **kwargs: missing_loc.generate_missing_loc_tool(
        settings,
        FocusIndex(fake_mod_root, cache_dir, None),
        EventIndex(fake_mod_root, cache_dir, None),
        LocalisationIndex(fake_mod_root, cache_dir, None, langs=("en",)),
        **kwargs,
    )


def test_generate_missing_loc_omits_existing_keys_and_returns_complete_text(
    fake_mod_root, cache_dir
):
    tool = _tool(fake_mod_root, cache_dir)

    result = tool(focus_ids=["TST_root"], event_ids=["TestnsNews.42"])

    assert result["ok"] is True
    assert result["complete"] is True
    assert result["partial"] is False
    assert result["total"] == 3
    assert [entry["key"] for entry in result["loc_yml_keys"]] == [
        "TestnsNews.42.a",
        "TestnsNews.42.d",
        "TestnsNews.42.t",
    ]
    assert "TST_root:" not in result["txt"]
    assert "TestnsNews.42.t:" in result["txt"]


def test_generate_missing_loc_marks_paged_and_unresolved_results_partial(fake_mod_root, cache_dir):
    tool = _tool(fake_mod_root, cache_dir)

    result = tool(event_ids=["TestnsNews.42"], focus_ids=["missing"], limit=1)

    assert result["complete"] is False
    assert result["partial"] is True
    assert result["truncated"] is True
    assert result["unresolved"] == ["focus:missing"]


def test_generate_missing_loc_marks_final_budget_truncation_partial(
    fake_mod_root, cache_dir, monkeypatch
):
    tool = _tool(fake_mod_root, cache_dir)
    original = missing_loc.enforce_budget

    def truncate(result, *, heavy_keys):
        return {**original(result, heavy_keys=heavy_keys), "size_truncated": True, "txt_dropped": 1}

    monkeypatch.setattr(missing_loc, "enforce_budget", truncate)
    result = tool(event_ids=["TestnsNews.42"])

    assert result["complete"] is False
    assert result["partial"] is True


def test_generate_missing_loc_uses_event_referenced_keys_and_marks_missing_fields(
    fake_mod_root, cache_dir
):
    (fake_mod_root / "events" / "test_events.txt").write_text(
        "country_event = {\n"
        "\tid = Testns.8\n"
        "\ttitle = custom_event_title\n"
        "\toption = { name = custom_event_option }\n"
        "}\n",
        encoding="utf-8",
    )
    result = _tool(fake_mod_root, cache_dir)(event_ids=["Testns.8"])

    assert [entry["key"] for entry in result["loc_yml_keys"]] == [
        "custom_event_option",
        "custom_event_title",
    ]
    assert result["complete"] is False
    assert result["partial"] is True
    assert result["failures"] == ["event:Testns.8: description localisation key is missing"]


def test_generate_missing_loc_collects_every_triggered_title_and_desc(fake_mod_root, cache_dir):
    (fake_mod_root / "events" / "triggered.txt").write_text(
        "country_event = {\n"
        "\tid = Testns.30\n"
        "\ttitle = { trigger = { has_war = yes } text = Testns.30.t0 }\n"
        "\ttitle = { trigger = { has_war = no } text = Testns.30.t1 }\n"
        "\tdesc = Testns.30.d\n"
        "\tdesc = { trigger = { has_war = yes } text = Testns.30.d_war }\n"
        '\tdesc = { trigger = { has_war = no } text = "[GetTestDesc]" }\n'
        "\toption = { name = Testns.30.a }\n"
        "}\n",
        encoding="utf-8",
    )
    result = _tool(fake_mod_root, cache_dir)(event_ids=["Testns.30"])

    assert [entry["key"] for entry in result["loc_yml_keys"]] == [
        "Testns.30.a",
        "Testns.30.d",
        "Testns.30.d_war",
        "Testns.30.t0",
        "Testns.30.t1",
    ]
    assert result["failures"] == []
    assert result["complete"] is True


def test_generate_missing_loc_reports_text_less_title_and_skips_hidden_fields(
    fake_mod_root, cache_dir
):
    (fake_mod_root / "events" / "odd_titles.txt").write_text(
        "country_event = {\n"
        "\tid = Testns.31\n"
        "\ttitle = { trigger = { has_war = yes } }\n"
        "\tdesc = Testns.31.d\n"
        "}\n"
        "country_event = {\n"
        "\tid = Testns.32\n"
        "\thidden = yes\n"
        "\toption = { name = Testns.32.a }\n"
        "}\n",
        encoding="utf-8",
    )
    result = _tool(fake_mod_root, cache_dir)(event_ids=["Testns.31", "Testns.32"])

    assert [entry["key"] for entry in result["loc_yml_keys"]] == ["Testns.31.d", "Testns.32.a"]
    assert result["failures"] == ["event:Testns.31: title has no scalar or text key"]
    assert result["partial"] is True


def test_generate_missing_loc_marks_generator_byte_budget_drops_partial(
    fake_mod_root, cache_dir, monkeypatch
):
    monkeypatch.setattr(FocusIndex, "resolve", lambda self, ident: {"tag": "TST"})
    monkeypatch.setattr(LocalisationIndex, "resolve", lambda self, key, lang="en": None)
    huge_id = "TST_" + "x" * 90_000

    result = _tool(fake_mod_root, cache_dir)(focus_ids=[huge_id])

    assert result["complete"] is False
    assert result["partial"] is True
    assert result["txt_truncated"] is True


def test_generate_missing_loc_reconciles_budget_dropped_keys(fake_mod_root, cache_dir, monkeypatch):
    monkeypatch.setattr(FocusIndex, "resolve", lambda self, ident: {"tag": "TST"})
    monkeypatch.setattr(LocalisationIndex, "resolve", lambda self, key, lang="en": None)
    monkeypatch.setattr(
        missing_loc,
        "generate_loc_stub",
        lambda *args, **kwargs: {"txt": "small generated stub\n"},
    )
    focus_ids = [f"TST_{number}_" + "x" * 1_000 for number in range(70)]

    result = _tool(fake_mod_root, cache_dir)(focus_ids=focus_ids)

    assert result["loc_yml_keys_dropped"] == 140
    assert result["total"] == 140
    assert result["returned"] == 0
    assert result["truncated"] is True
    assert result["complete"] is False
    assert result["partial"] is True
    assert len(json.dumps(result).encode("utf-8")) <= 100_000


def test_generate_missing_loc_marks_structured_option_key_partial(fake_mod_root, cache_dir):
    (fake_mod_root / "events" / "structured_option.txt").write_text(
        "country_event = {\n"
        "  id = Testns.90\n"
        "  title = Testns.90.title\n"
        "  desc = Testns.90.desc\n"
        "  option = { name = { text = Testns.90.option } }\n"
        "}\n",
        encoding="utf-8",
    )
    result = _tool(fake_mod_root, cache_dir)(event_ids=["Testns.90"])
    assert result["complete"] is False
    assert result["partial"] is True
    assert any("option localisation key is non-scalar" in item for item in result["failures"])


@pytest.mark.parametrize("failure", ["missing", "parse"])
def test_generate_missing_loc_marks_unavailable_or_unreadable_event_source_partial(
    fake_mod_root, cache_dir, monkeypatch, failure
):
    from md_mcp.generators import missing_loc as module

    source = fake_mod_root / "events" / "broken.txt"
    source.write_text("broken", encoding="utf-8")
    monkeypatch.setattr(
        EventIndex,
        "resolve",
        lambda self, ident: {"file": "events/broken.txt", "line": 1},
    )
    if failure == "missing":
        monkeypatch.setattr(module, "resolve_scope_file", lambda *args: None)
    else:
        monkeypatch.setattr(module, "resolve_scope_file", lambda *args: source)
        monkeypatch.setattr(module, "parse_cached", lambda path: (_ for _ in ()).throw(OSError()))
    result = _tool(fake_mod_root, cache_dir)(event_ids=["Testns.91"])
    assert result["complete"] is False
    assert result["partial"] is True
    assert result["failures"]
