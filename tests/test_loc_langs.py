"""Localisation language scoping: settings, indexed set, and the on-demand fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from md_mcp import config
from md_mcp.config import Settings
from md_mcp.indexes import LocalisationIndex
from md_mcp.indexes.localisation import LANG_ISO_TO_SUFFIX, normalise_loc_langs
from md_mcp.tools.resolver_tools import resolve_loc_tool

GERMAN = 'l_german:\n TST_root: "Die Wurzel"\n TST_only_de: "Nur Deutsch"\n'
FRENCH = 'l_french:\n TST_root: "La Racine"\n'


def _add_langs(root: Path) -> None:
    de = root / "localisation" / "german"
    fr = root / "localisation" / "french"
    de.mkdir(parents=True)
    fr.mkdir(parents=True)
    (de / "test_l_german.yml").write_text(GERMAN, encoding="utf-8")
    (fr / "test_l_french.yml").write_text(FRENCH, encoding="utf-8")


@pytest.fixture
def multi_lang_root(fake_mod_root: Path) -> Path:
    _add_langs(fake_mod_root)
    return fake_mod_root


def _make_mod_root(path: Path) -> Path:
    (path / "tools" / "validation").mkdir(parents=True)
    (path / "descriptor.mod").write_text('name = "x"\n', encoding="utf-8")
    return path


def _settings(tmp_path: Path, monkeypatch, toml: str | None = None) -> Settings:
    root = _make_mod_root(tmp_path / "Mod")
    cfg = tmp_path / "config.toml"
    if toml is not None:
        cfg.write_text(toml, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    for var in ("MD_MCP_LOC_LANGS", "MD_MCP_DEFAULT_LANG"):
        monkeypatch.delenv(var, raising=False)
    return config.load(str(root))


# ----- settings --------------------------------------------------------------


def test_settings_default_loc_langs_is_default_lang():
    assert Settings(Path("."), None, Path(".")).loc_langs == ("en",)
    assert Settings(Path("."), None, Path("."), default_lang="DE").loc_langs == ("de",)


def test_load_defaults_loc_langs_to_default_lang(tmp_path, monkeypatch):
    assert _settings(tmp_path, monkeypatch).loc_langs == ("en",)
    monkeypatch.setenv("MD_MCP_DEFAULT_LANG", "fr")
    root = _make_mod_root(tmp_path / "Mod2")
    assert config.load(str(root)).loc_langs == ("fr",)


def test_load_reads_loc_langs_from_env(tmp_path, monkeypatch):
    root = _make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_LOC_LANGS", "en, de")
    assert config.load(str(root)).loc_langs == ("en", "de")


def test_load_reads_loc_langs_from_toml_string_and_list(tmp_path, monkeypatch):
    assert _settings(tmp_path, monkeypatch, 'loc_langs = "en,de"\n').loc_langs == ("en", "de")
    root = _make_mod_root(tmp_path / "Mod3")
    cfg = tmp_path / "config3.toml"
    cfg.write_text('loc_langs = ["fr", "pt-br"]\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    assert config.load(str(root)).loc_langs == ("fr", "pt-br")


def test_env_loc_langs_wins_over_toml(tmp_path, monkeypatch):
    root = _make_mod_root(tmp_path / "Mod")
    cfg = tmp_path / "config.toml"
    cfg.write_text('loc_langs = "de"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setenv("MD_MCP_LOC_LANGS", "fr")
    assert config.load(str(root)).loc_langs == ("fr",)


def test_loc_langs_star_means_all(tmp_path, monkeypatch):
    root = _make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_LOC_LANGS", "*")
    assert config.load(str(root)).loc_langs == tuple(LANG_ISO_TO_SUFFIX)


def test_unknown_loc_lang_fails_loudly(tmp_path, monkeypatch):
    root = _make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_LOC_LANGS", "en,klingon")
    with pytest.raises(RuntimeError, match="klingon"):
        config.load(str(root))


def test_normalise_loc_langs_dedupes_and_lowercases():
    assert normalise_loc_langs("EN,de,en") == ("en", "de")
    assert normalise_loc_langs(None, default="fr") == ("fr",)
    assert normalise_loc_langs(["de", "*"]) == tuple(LANG_ISO_TO_SUFFIX)
    # Lenient mode drops what it can't place instead of raising.
    assert normalise_loc_langs("xx,de") == ("de",)


# ----- indexed set -----------------------------------------------------------


def test_default_indexes_only_english(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    assert li.list_files() == ["localisation/english/test_l_english.yml"]
    assert {lang for lang, _ in li._by_key} == {"l_english"}


def test_langs_en_de_indexes_both(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False, langs="en,de")
    assert li.list_files() == [
        "localisation/english/test_l_english.yml",
        "localisation/german/test_l_german.yml",
    ]
    assert {lang for lang, _ in li._by_key} == {"l_english", "l_german"}
    hit = li.resolve("TST_root", "de")
    assert hit is not None and hit["value"] == "Die Wurzel" and hit["lang"] == "de"
    assert not li._scans, "an indexed language must not use the on-demand scan"


def test_langs_star_indexes_all(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False, langs="*")
    assert len(li.list_files()) == 3
    assert li.resolve("TST_root", "fr")["value"] == "La Racine"  # type: ignore[index]


def test_langs_without_english_still_falls_back_to_english(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False, langs=["de"])
    assert li.list_files() == ["localisation/german/test_l_german.yml"]
    # Key only English has: found through the English on-demand fallback.
    hit = li.resolve("TST_branch_a", "de")
    assert hit is not None and hit["value"] == "Branch A" and hit["lang"] == "en"


def test_changing_langs_reuses_cache_and_drops_removed_language(multi_lang_root, cache_dir):
    en = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    en.ensure_fresh()
    both = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False, langs="en,de")
    assert len(both.list_files()) == 2
    shard_dir = both._cache.shard_dir
    assert len(list(shard_dir.iterdir())) == 2

    back = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    assert back.list_files() == ["localisation/english/test_l_english.yml"]
    assert len(list(shard_dir.iterdir())) == 1


# ----- on-demand fallback ----------------------------------------------------


def test_resolve_unindexed_language_via_scan(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    hit = li.resolve("TST_root", lang="de")
    assert hit is not None
    assert hit["value"] == "Die Wurzel"
    assert hit["lang"] == "de"
    assert hit["file"] == "localisation/german/test_l_german.yml"
    assert hit["line"] == 2
    # Not persisted and not in the index.
    assert li.list_files() == ["localisation/english/test_l_english.yml"]
    assert not any("german" in p.name for p in li._cache.shard_dir.iterdir())


def test_resolve_unindexed_language_missing_key_falls_back_to_english(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    hit = li.resolve("TST_branch_a", lang="de")
    assert hit is not None
    assert hit["value"] == "Branch A"
    assert hit["lang"] == "en"
    assert li.resolve("NOPE_nothing", lang="de") is None


def test_resolve_unindexed_language_without_any_files_is_english_fallback(fake_mod_root, cache_dir):
    li = LocalisationIndex(fake_mod_root, cache_dir, include_vanilla=False)
    hit = li.resolve("TST_root", lang="ru")
    assert hit is not None and hit["lang"] == "en"


def test_scan_invalidates_when_language_file_changes(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    assert li.resolve("TST_root", "de")["value"] == "Die Wurzel"  # type: ignore[index]

    de_file = multi_lang_root / "localisation" / "german" / "test_l_german.yml"
    de_file.write_text('l_german:\n TST_root: "Neue Wurzel"\n', encoding="utf-8")
    li._scans["l_german"].stale_check.force_next()

    assert li.resolve("TST_root", "de")["value"] == "Neue Wurzel"  # type: ignore[index]
    # The key that vanished with the edit no longer resolves in German.
    assert li.resolve("TST_only_de", "de") is None


def test_scan_picks_up_new_and_removed_files_and_reuses_unchanged(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    li.resolve("TST_root", "de")
    scan = li._scans["l_german"]
    first_records = scan.files["localisation/german/test_l_german.yml"]

    extra = multi_lang_root / "localisation" / "german" / "extra_l_german.yml"
    extra.write_text('l_german:\n TST_extra: "Extra"\n', encoding="utf-8")
    scan.stale_check.force_next()
    assert li.resolve("TST_extra", "de")["value"] == "Extra"  # type: ignore[index]
    # Unchanged file was not re-parsed.
    assert scan.files["localisation/german/test_l_german.yml"] is first_records

    extra.unlink()
    scan.stale_check.force_next()
    assert li.resolve("TST_extra", "de") is None


def test_scan_is_debounced(multi_lang_root, cache_dir, monkeypatch):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    li.resolve("TST_root", "de")
    calls: list[int] = []
    import md_mcp.indexes.localisation as loc_mod

    real = loc_mod.collect_files

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(loc_mod, "collect_files", spy)
    li.resolve("TST_root", "de")
    assert calls == []


def test_list_keys_for_unindexed_language(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    assert li.list_keys("de") == ["TST_only_de", "TST_root"]
    assert li.list_keys("klingon") == []


def test_resolve_loc_tool_uses_scan_for_unindexed_language(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    settings = Settings(multi_lang_root, None, cache_dir)
    result = resolve_loc_tool("TST_only_de", settings, li, "de")
    assert result["ok"] is True
    assert result["value"] == "Nur Deutsch"
    assert result["lang"] == "de"
