"""Localisation language scoping: settings, indexed set, and the on-demand fallback."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from md_mcp import config
from md_mcp.config import Settings
from md_mcp.indexes import LocalisationIndex
from md_mcp.indexes import localisation as loc_mod
from md_mcp.indexes.base import collect_files
from md_mcp.indexes.localisation import LANG_ISO_TO_SUFFIX, cache_name_for, normalise_loc_langs
from md_mcp.tools.resolver_tools import resolve_loc_tool
from md_mcp.util.encoding import read_text
from tests.support.indexes import bump_mtime, write_loc
from tests.support.mod import make_mod_root

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


def _settings(tmp_path: Path, monkeypatch, toml: str | None = None) -> Settings:
    root = make_mod_root(tmp_path / "Mod")
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
    assert Settings(Path("."), None, Path("."), default_lang="DE").loc_langs == ("de", "en")


def test_load_defaults_loc_langs_to_default_lang(tmp_path, monkeypatch):
    assert _settings(tmp_path, monkeypatch).loc_langs == ("en",)
    monkeypatch.setenv("MD_MCP_DEFAULT_LANG", "fr")
    root = make_mod_root(tmp_path / "Mod2")
    assert config.load(str(root)).loc_langs == ("fr", "en")


def test_load_reads_loc_langs_from_env(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_LOC_LANGS", "en, de")
    assert config.load(str(root)).loc_langs == ("en", "de")


def test_load_reads_loc_langs_from_toml_string_and_list(tmp_path, monkeypatch):
    assert _settings(tmp_path, monkeypatch, 'loc_langs = "en,de"\n').loc_langs == ("en", "de")
    root = make_mod_root(tmp_path / "Mod3")
    cfg = tmp_path / "config3.toml"
    cfg.write_text('loc_langs = ["fr", "pt-br"]\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    assert config.load(str(root)).loc_langs == ("fr", "pt-br", "en")


def test_env_loc_langs_wins_over_toml(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    cfg = tmp_path / "config.toml"
    cfg.write_text('loc_langs = "de"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setenv("MD_MCP_LOC_LANGS", "fr")
    assert config.load(str(root)).loc_langs == ("fr", "en")


def test_loc_langs_star_means_all(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_LOC_LANGS", "*")
    assert config.load(str(root)).loc_langs == tuple(LANG_ISO_TO_SUFFIX)


def test_unknown_loc_lang_fails_loudly(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_LOC_LANGS", "en,klingon")
    with pytest.raises(RuntimeError, match="klingon"):
        config.load(str(root))


def test_unknown_default_lang_does_not_abort_startup(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.delenv("MD_MCP_LOC_LANGS", raising=False)
    monkeypatch.setenv("MD_MCP_DEFAULT_LANG", "english")

    settings = config.load(str(root))

    assert settings.default_lang == "english"
    assert settings.loc_langs == ("en",)


@pytest.mark.parametrize(
    ("env", "toml"),
    [
        pytest.param("klingon", None, id="unknown-language-in-env"),
        pytest.param(None, "loc_langs = 1\n", id="non-iterable-in-toml"),
    ],
)
def test_invalid_explicit_loc_langs_is_a_config_error(tmp_path, monkeypatch, env, toml):
    root = make_mod_root(tmp_path / "Mod")
    cfg = tmp_path / "config.toml"
    if toml is not None:
        cfg.write_text(toml, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    if env is None:
        monkeypatch.delenv("MD_MCP_LOC_LANGS", raising=False)
    else:
        monkeypatch.setenv("MD_MCP_LOC_LANGS", env)

    with pytest.raises(RuntimeError, match="Invalid loc_langs"):
        config.load(str(root))


def test_normalise_loc_langs_dedupes_and_lowercases():
    assert normalise_loc_langs("EN,de,en") == ("en", "de")
    assert normalise_loc_langs(None, default="fr") == ("fr",)
    assert normalise_loc_langs(["de", "*"]) == tuple(LANG_ISO_TO_SUFFIX)
    # Lenient mode drops what it can't place instead of raising.
    assert normalise_loc_langs("xx,de") == ("de",)


def test_cache_name_is_independent_of_language_order():
    assert cache_name_for(("en", "de")) == cache_name_for(("de", "en")) == "loc-de_en"
    assert cache_name_for(("pt-br", "en")) == "loc-en_ptbr"


# ----- indexed set -----------------------------------------------------------


def test_default_indexes_only_english(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    assert li.list_files() == [str(Path("localisation/english/test_l_english.yml"))]
    assert {lang for lang, _ in li._by_key} == {"l_english"}


def test_langs_en_de_indexes_both(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False, langs="en,de")
    assert li.list_files() == [
        str(Path("localisation/english/test_l_english.yml")),
        str(Path("localisation/german/test_l_german.yml")),
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
    assert li.list_files() == [str(Path("localisation/german/test_l_german.yml"))]
    # Key only English has: found through the English on-demand fallback.
    hit = li.resolve("TST_branch_a", "de")
    assert hit is not None and hit["value"] == "Branch A" and hit["lang"] == "en"


def test_changing_langs_uses_a_separate_cache_per_language_set(multi_lang_root, cache_dir):
    en = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    en.ensure_fresh()
    both = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False, langs="en,de")
    assert len(both.list_files()) == 2
    assert both._cache.shard_dir != en._cache.shard_dir
    assert len(list(both._cache.shard_dir.iterdir())) == 2

    # Going back to English alone neither touches nor trusts the en+de cache.
    back = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    assert back.list_files() == [str(Path("localisation/english/test_l_english.yml"))]
    assert back._cache.shard_dir == en._cache.shard_dir
    assert len(list(both._cache.shard_dir.iterdir())) == 2
    assert len(list(back._cache.shard_dir.iterdir())) == 1


def test_language_sets_do_not_share_a_cache(tmp_path):
    """A process with fewer languages must not delete another's shards."""
    root = make_mod_root(tmp_path / "Mod")
    cache = tmp_path / "cache"
    write_loc(root, "localisation/english/a_l_english.yml", "l_english", "K_EN", "e")
    write_loc(root, "localisation/german/a_l_german.yml", "l_german", "K_DE", "d")
    both = LocalisationIndex(root, cache, None, langs=("en", "de"))
    both.ensure_fresh()
    shards_before = sorted(p.name for p in both._cache.shard_dir.iterdir())
    assert len(shards_before) == 2

    LocalisationIndex(root, cache, None, langs=("en",)).ensure_fresh()
    assert sorted(p.name for p in both._cache.shard_dir.iterdir()) == shards_before

    again = LocalisationIndex(root, cache, None, langs=("en", "de"))
    parsed: list[list[str]] = []
    real = again._parse_results

    def spy(relpaths):
        parsed.append(list(relpaths))
        return real(relpaths)

    again._parse_results = spy  # type: ignore[method-assign]
    again.ensure_fresh()
    assert parsed in ([], [[]])
    assert again.resolve("K_DE", "de") is not None


# ----- on-demand fallback ----------------------------------------------------


def test_resolve_unindexed_language_via_scan(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    hit = li.resolve("TST_root", lang="de")
    assert hit is not None
    assert hit["value"] == "Die Wurzel"
    assert hit["lang"] == "de"
    assert hit["file"] == str(Path("localisation/german/test_l_german.yml"))
    assert hit["line"] == 2
    # Not persisted and not in the index.
    assert li.list_files() == [str(Path("localisation/english/test_l_english.yml"))]
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
    first_records = scan.files[str(Path("localisation/german/test_l_german.yml"))]

    extra = multi_lang_root / "localisation" / "german" / "extra_l_german.yml"
    extra.write_text('l_german:\n TST_extra: "Extra"\n', encoding="utf-8")
    scan.stale_check.force_next()
    assert li.resolve("TST_extra", "de")["value"] == "Extra"  # type: ignore[index]
    # Unchanged file was not re-parsed.
    assert scan.files[str(Path("localisation/german/test_l_german.yml"))] is first_records

    extra.unlink()
    scan.stale_check.force_next()
    assert li.resolve("TST_extra", "de") is None


def test_scan_is_debounced(multi_lang_root, cache_dir, monkeypatch):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    li.resolve("TST_root", "de")
    scan = li._scans["l_german"]
    records = scan.files[str(Path("localisation/german/test_l_german.yml"))]
    calls: list[int] = []
    real = collect_files

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(loc_mod, "collect_files", spy)
    li.resolve("TST_root", "de")
    assert calls == []

    # Past the debounce it stats the files again, and reuses the scan when none moved.
    scan.stale_check.force_next()
    li.resolve("TST_root", "de")
    assert calls == [1]
    assert scan.files[str(Path("localisation/german/test_l_german.yml"))] is records


def test_scan_lang_survives_a_file_that_failed_to_parse(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    good = write_loc(root, "localisation/german/a_l_german.yml", "l_german", "A", "a")
    bad = write_loc(root, "localisation/german/b_l_german.yml", "l_german", "B", "b")
    index = LocalisationIndex(root, tmp_path / "cache", None, langs=("en",))

    real_read = read_text

    def flaky_read(path):
        if Path(path).name == bad.name:
            raise OSError("locked")
        return real_read(path)

    monkeypatch.setattr(loc_mod, "read_text", flaky_read)
    assert index.resolve("A", "de") is not None
    assert index.resolve("B", "de") is None  # unreadable, so absent, not an error

    # Edit another file past the debounce window: the refresh must not KeyError on
    # the file that has a signature but no records.
    bump_mtime(good)
    index._scans["l_german"].stale_check.force_next()
    assert index.resolve("A", "de") is not None

    # Once readable again it is picked up on the next refresh.
    monkeypatch.setattr(loc_mod, "read_text", real_read)
    index._scans["l_german"].stale_check.force_next()
    bump_mtime(bad)
    assert index.resolve("B", "de") is not None


def test_list_keys_per_language(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    assert li.list_keys("de") == ["TST_only_de", "TST_root"]
    assert li.list_keys("klingon") == []
    assert "TST_root" in li.list_keys()
    assert "TST_only_de" not in li.list_keys()


def test_resolve_in_an_unknown_language_is_a_miss(fake_mod_root, cache_dir):
    li = LocalisationIndex(fake_mod_root, cache_dir, include_vanilla=False)
    assert li.resolve("TST_root", "klingon") is None


def test_resolve_loc_tool_uses_scan_for_unindexed_language(multi_lang_root, cache_dir):
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    settings = Settings(multi_lang_root, None, cache_dir)
    result = resolve_loc_tool("TST_only_de", settings, li, "de")
    assert result["ok"] is True
    assert result["value"] == "Nur Deutsch"
    assert result["lang"] == "de"


# ----- concurrent first scan ----------------------------------------------------------


@pytest.mark.parametrize("second_kind", ["resolve", "list_keys", "other_language"])
def test_first_scan_coordinates_concurrent_language_lookups(
    multi_lang_root, cache_dir, monkeypatch, second_kind
):
    """Same-language callers wait; unrelated warm lookups do not."""
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    li.list_keys()
    if second_kind == "other_language":
        assert li.resolve("TST_root", "fr") is not None

    scan_started = threading.Event()
    release_scan = threading.Event()
    second_waiting = threading.Event()
    second_done = threading.Event()
    real_read = read_text

    def pausing_read(path):
        if Path(path).name == "test_l_german.yml":
            scan_started.set()
            assert release_scan.wait(timeout=10), "first scan was never released"
        return real_read(path)

    monkeypatch.setattr(loc_mod, "read_text", pausing_read)

    first_hits: list[dict | None] = []
    second_missing: list[dict | None] = []
    second_root: list[dict | None] = []
    second_keys: list[list[str]] = []
    errors: list[BaseException] = []

    def first() -> None:
        try:
            first_hits.append(li.resolve("TST_root", "de"))
        except BaseException as exc:  # reported on the main thread below
            errors.append(exc)

    def second() -> None:
        try:
            if second_kind == "resolve":
                second_missing.append(li.resolve("TST_only_de", "de"))
                second_root.append(li.resolve("TST_root", "de"))
            elif second_kind == "list_keys":
                second_keys.append(li.list_keys("de"))
            else:
                second_root.append(li.resolve("TST_root", "fr"))
        except BaseException as exc:  # reported on the main thread below
            errors.append(exc)
        second_done.set()

    first_thread = threading.Thread(target=first, daemon=True)
    second_thread = threading.Thread(target=second, daemon=True)
    try:
        first_thread.start()
        assert scan_started.wait(timeout=10), "first scan never reached the German file"
        lock = li._scans["l_german"].lock

        class ObservedLock:
            def __enter__(self):
                if not lock.acquire(blocking=False):
                    second_waiting.set()
                    lock.acquire()

            def __exit__(self, *args):
                lock.release()

        monkeypatch.setattr(li._scans["l_german"], "lock", ObservedLock())
        second_thread.start()
        if second_kind == "other_language":
            assert second_done.wait(timeout=5), "unrelated warm lookup was blocked"
        else:
            assert second_waiting.wait(timeout=10), "second lookup never waited for the scan"
            assert not second_done.is_set(), "second lookup saw the unfinished scan"
    finally:
        release_scan.set()
        first_thread.join(timeout=10)
        if second_thread.ident is not None:
            second_thread.join(timeout=10)
    assert not first_thread.is_alive(), "first scan did not finish"
    assert not second_thread.is_alive(), "second lookup did not finish"
    assert errors == []

    assert len(first_hits) == 1
    first_hit = first_hits[0]
    assert first_hit is not None
    assert first_hit["value"] == "Die Wurzel" and first_hit["lang"] == "de"
    if second_kind == "resolve":
        assert len(second_missing) == 1 and len(second_root) == 1
        missing = second_missing[0]
        root = second_root[0]
        assert missing is not None
        assert missing["value"] == "Nur Deutsch" and missing["lang"] == "de"
        assert root is not None
        assert root["value"] == "Die Wurzel" and root["lang"] == "de"
    elif second_kind == "list_keys":
        assert second_keys == [["TST_only_de", "TST_root"]]
    else:
        assert len(second_root) == 1
        french = second_root[0]
        assert french is not None
        assert french["value"] == "La Racine" and french["lang"] == "fr"


def test_failed_first_scan_releases_the_lock_and_retries(multi_lang_root, cache_dir, monkeypatch):
    """A first load that raises must not strand callers or pin the empty scan."""
    li = LocalisationIndex(multi_lang_root, cache_dir, include_vanilla=False)
    real_collect = collect_files
    calls: list[int] = []

    def flaky_collect(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("directory gone")
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(loc_mod, "collect_files", flaky_collect)

    with pytest.raises(OSError, match="directory gone"):
        li.resolve("TST_root", "de")

    scan = li._scans["l_german"]
    assert scan.loaded is False, "a failed load must not publish results"
    assert not scan.lock.locked(), "a failed load must release the scan lock"

    hit = li.resolve("TST_root", "de")
    assert hit is not None
    assert hit["value"] == "Die Wurzel" and hit["lang"] == "de"
    assert calls == [1, 1]
    assert scan.loaded is True
