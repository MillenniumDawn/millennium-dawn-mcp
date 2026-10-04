"""Regressions found in review of the warm-path performance changes."""

from __future__ import annotations

import os
from pathlib import Path

from md_mcp import config
from md_mcp.analysis.manifest import _loc_files
from md_mcp.indexes import localisation as loc_mod
from md_mcp.indexes.localisation import LocalisationIndex

_LOC = '\ufeffl_{lang}:\n {key}:0 "{value}"\n'


def _mod_root(tmp_path: Path) -> Path:
    root = tmp_path / "Mod"
    (root / "tools" / "validation").mkdir(parents=True)
    (root / "descriptor.mod").write_text('name = "x"\n', encoding="utf-8")
    return root


def _write_loc(root: Path, rel: str, lang: str, key: str, value: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_LOC.format(lang=lang, key=key, value=value), encoding="utf-8")
    return p


def test_scan_lang_survives_a_file_that_failed_to_parse(tmp_path, monkeypatch):
    root = _mod_root(tmp_path)
    good = _write_loc(root, "localisation/german/a_l_german.yml", "l_german", "A", "a")
    bad = _write_loc(root, "localisation/german/b_l_german.yml", "l_german", "B", "b")
    index = LocalisationIndex(root, tmp_path / "cache", None, langs=("en",))

    real_read = loc_mod.read_text

    def flaky_read(path):
        if Path(path).name == bad.name:
            raise OSError("locked")
        return real_read(path)

    monkeypatch.setattr(loc_mod, "read_text", flaky_read)
    assert index.resolve("A", "de") is not None
    assert index.resolve("B", "de") is None  # unreadable, so absent, not an error

    # Edit another file past the debounce window: the refresh must not KeyError on
    # the file that has a signature but no records.
    st = os.stat(good)
    os.utime(good, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    index._scans["l_german"].stale_check.force_next()
    assert index.resolve("A", "de") is not None

    # Once readable again it is picked up on the next refresh.
    monkeypatch.setattr(loc_mod, "read_text", real_read)
    index._scans["l_german"].stale_check.force_next()
    st = os.stat(bad)
    os.utime(bad, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert index.resolve("B", "de") is not None


def test_unknown_default_lang_does_not_abort_startup(tmp_path, monkeypatch):
    root = _mod_root(tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.delenv("MD_MCP_LOC_LANGS", raising=False)
    monkeypatch.setenv("MD_MCP_DEFAULT_LANG", "english")

    settings = config.load(str(root))

    assert settings.default_lang == "english"
    assert settings.loc_langs == ("en",)


def test_explicit_unknown_loc_langs_still_rejected(tmp_path, monkeypatch):
    root = _mod_root(tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_LOC_LANGS", "klingon")
    try:
        config.load(str(root))
    except RuntimeError as exc:
        assert "Invalid loc_langs" in str(exc)
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("expected RuntimeError")


def test_non_iterable_toml_loc_langs_is_a_config_error(tmp_path, monkeypatch):
    root = _mod_root(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("loc_langs = 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.delenv("MD_MCP_LOC_LANGS", raising=False)
    try:
        config.load(str(root))
    except RuntimeError as exc:
        assert "Invalid loc_langs" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


def test_manifest_loc_files_cover_every_language(tmp_path):
    root = _mod_root(tmp_path)
    _write_loc(root, "localisation/english/MD_focus_USA_l_english.yml", "l_english", "K", "v")
    _write_loc(root, "localisation/german/MD_focus_USA_l_german.yml", "l_german", "K", "v")
    _write_loc(root, "localisation/english/MD_focus_ISR_l_english.yml", "l_english", "K", "v")

    files = _loc_files(root, "USA")

    assert files == sorted(
        [
            str(Path("localisation/english/MD_focus_USA_l_english.yml")),
            str(Path("localisation/german/MD_focus_USA_l_german.yml")),
        ]
    )
