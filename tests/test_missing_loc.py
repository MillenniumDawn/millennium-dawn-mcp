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


def test_file_route_validates_selector_and_language_arguments(fake_mod_root, cache_dir):
    tool = _tool(fake_mod_root, cache_dir)

    assert tool()["error"] == "Provide file=, focus_ids= or event_ids=."
    assert tool(target_file="localisation/english/new_l_english.yml")["error"] == (
        "target_file= requires file=."
    )
    assert tool(file="common/national_focus/test.txt", lang="de")["error"].endswith("(lang='en').")
    mixed = tool(file="common/national_focus/test.txt", focus_ids=["TST_root"])
    assert mixed["error"] == "Use file= or focus_ids=/event_ids=, not both."


def test_file_route_reports_missing_unreadable_and_unsupported_sources(
    fake_mod_root, cache_dir, monkeypatch
):
    tool = _tool(fake_mod_root, cache_dir)
    assert tool(file="common/national_focus/missing.txt")["error"].startswith(
        "Source file not found"
    )

    source = fake_mod_root / "common" / "national_focus" / "bad_parse.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("focus_tree = { }", encoding="utf-8")
    monkeypatch.setattr(
        missing_loc,
        "parse_cached",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("fixture parse error")),
    )
    assert tool(file="common/national_focus/bad_parse.txt")["error"] == (
        "Could not parse source file (ValueError)."
    )
    monkeypatch.undo()

    unsupported = fake_mod_root / "common" / "scripted_effects" / "unsupported.txt"
    unsupported.parent.mkdir(parents=True, exist_ok=True)
    unsupported.write_text("effect = { add_stability = 0.1 }", encoding="utf-8")
    assert tool(file="common/scripted_effects/unsupported.txt")["error"].startswith(
        "Source file type is unsupported"
    )


def test_file_route_marks_missing_event_fields_partial(fake_mod_root, cache_dir):
    source = fake_mod_root / "events" / "missing_fields_route.txt"
    source.write_text(
        "add_namespace = TST\ncountry_event = { id = TST.4 title = TST.4.t }",
        encoding="utf-8",
    )
    result = _tool(fake_mod_root, cache_dir)(file="events/missing_fields_route.txt")
    assert result["complete"] is False
    assert result["partial"] is True
    assert result["failures"] == ["event:TST.4: desc localisation key is missing"]


def test_file_route_skips_target_keys_and_derives_header_and_bom(fake_mod_root, cache_dir):
    source = fake_mod_root / "common" / "national_focus" / "file_route.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "focus_tree = { id = TST_file_route country = { factor = 0 } "
        "focus = { id = TST_file_route_new x = 0 y = 0 } }\n",
        encoding="utf-8",
    )
    target = fake_mod_root / "localisation" / "english" / "file_route_l_english.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\ufeffl_english:\n TST_file_route_new: "Already here"\n', encoding="utf-8")

    result = _tool(fake_mod_root, cache_dir)(
        file="common/national_focus/file_route.txt",
        target_file="localisation/english/file_route_l_english.yml",
    )
    assert result["ok"] is True
    assert result["target_exists"] is True
    assert result["bom_prefix"] is False
    assert result["txt"].startswith("l_english:") is False
    assert " TST_file_route_new:" not in result["txt"]
    assert "TST_file_route_new_desc" in result["txt"]
    assert (
        target.read_text(encoding="utf-8-sig")
        == 'l_english:\n TST_file_route_new: "Already here"\n'
    )

    new_result = _tool(fake_mod_root, cache_dir)(file="common/national_focus/file_route.txt")
    assert new_result["bom_prefix"] is True
    assert new_result["txt"].startswith("\ufeffl_english:")


def test_file_route_validates_and_reads_target_paths(fake_mod_root, cache_dir):
    source = fake_mod_root / "common" / "national_focus" / "target_path_route.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "focus_tree = { id = TST_target_route focus = { id = TST_target_route x = 0 y = 0 } }",
        encoding="utf-8",
    )
    tool = _tool(fake_mod_root, cache_dir)

    invalid_name = tool(file="common/national_focus/target_path_route.txt", target_file="loc.yml")
    assert invalid_name["error"].startswith("target_file must be an English localisation path")

    escapes = tool(
        file="common/national_focus/target_path_route.txt",
        target_file="localisation/english/../../../outside_l_english.yml",
    )
    assert escapes["error"] == "target_file escapes configured content roots."

    new_target = tool(
        file="common/national_focus/target_path_route.txt",
        target_file="localisation/english/new_l_english.yml",
    )
    assert new_target["ok"] is True
    assert new_target["target_exists"] is False
    assert new_target["bom_prefix"] is True

    directory_target = fake_mod_root / "localisation" / "english" / "directory_l_english.yml"
    directory_target.mkdir(parents=True)
    unreadable = tool(
        file="common/national_focus/target_path_route.txt",
        target_file="localisation/english/directory_l_english.yml",
    )
    assert unreadable["error"] == "Could not read target localisation file (IsADirectoryError)."


@pytest.mark.parametrize(
    ("relative", "text", "expected"),
    [
        (
            "common/ideas/route_ideas.txt",
            "ideas = { political = { TST_new_idea = { picture = generic_idea } } }",
            "TST_new_idea_desc",
        ),
        (
            "common/decisions/route_decisions.txt",
            "route_category = { TST_new_decision = { cost = 10 } }",
            "TST_new_decision_desc",
        ),
        (
            "common/decisions/categories/route_categories.txt",
            "route_category = { icon = GFX_decision }",
            "route_category_desc",
        ),
    ],
)
def test_file_route_supports_idea_decision_and_category_keys(
    fake_mod_root, cache_dir, relative, text, expected
):
    source = fake_mod_root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text, encoding="utf-8")
    result = _tool(fake_mod_root, cache_dir)(file=relative)
    assert result["ok"] is True
    assert expected in result["txt"]


def test_file_route_rejects_invalid_target_path(fake_mod_root, cache_dir):
    source = fake_mod_root / "events" / "route_event.txt"
    source.write_text(
        "add_namespace = TST\ncountry_event = { id = TST.1 title = TST.1.t desc = TST.1.d }",
        encoding="utf-8",
    )
    result = _tool(fake_mod_root, cache_dir)(
        file="events/route_event.txt", target_file="../outside_l_english.yml"
    )
    assert result["ok"] is False


def test_file_route_marks_non_scalar_event_and_offset_results_partial(fake_mod_root, cache_dir):
    source = fake_mod_root / "events" / "partial_route.txt"
    source.write_text(
        "add_namespace = TST\ncountry_event = { id = TST.2 "
        "title = { trigger = { has_war = yes } } desc = TST.2.d "
        "option = { name = { text = TST.2.a } } }",
        encoding="utf-8",
    )
    result = _tool(fake_mod_root, cache_dir)(file="events/partial_route.txt", offset=1)
    assert result["complete"] is False
    assert result["partial"] is True
    assert any("no scalar or text key" in item for item in result["failures"])


def test_file_route_skips_scripted_localisation_calls(fake_mod_root, cache_dir):
    source = fake_mod_root / "events" / "scripted_loc_route.txt"
    source.write_text(
        "add_namespace = TST\n"
        "country_event = { id = TST.3 "
        'title = { trigger = { has_war = yes } text = "[GetTitle]" } '
        'desc = TST.3.d option = { name = "[GetOption]" } }',
        encoding="utf-8",
    )
    result = _tool(fake_mod_root, cache_dir)(file="events/scripted_loc_route.txt")
    assert result["ok"] is True
    assert result["complete"] is True
    assert "[GetTitle]" not in result["txt"]
    assert "[GetOption]" not in result["txt"]
    assert "TST.3.d" in result["txt"]


@pytest.mark.parametrize("drop_kind", ["size", "text", "keys"])
def test_file_route_reconciles_final_budget_drops(fake_mod_root, cache_dir, monkeypatch, drop_kind):
    source = fake_mod_root / "common" / "national_focus" / f"budget_{drop_kind}.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        f"focus_tree = {{ id = TST_budget focus = {{ id = TST_{drop_kind} x = 0 y = 0 }} }}",
        encoding="utf-8",
    )
    calls = 0

    def simulate_budget(result, *, heavy_keys):
        nonlocal calls
        calls += 1
        if calls > 1:
            return result
        if drop_kind == "size":
            return {**result, "size_truncated": True}
        if drop_kind == "text":
            return {**result, "txt_dropped": 1}
        return {**result, "loc_yml_keys_dropped": 2}

    monkeypatch.setattr(missing_loc, "enforce_budget", simulate_budget)
    result = _tool(fake_mod_root, cache_dir)(file=f"common/national_focus/budget_{drop_kind}.txt")
    assert result["complete"] is False
    assert result["partial"] is True
    if drop_kind == "text":
        assert result["txt_truncated"] is True
    if drop_kind == "keys":
        assert result["returned"] == 0
        assert result["truncated"] is True


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
