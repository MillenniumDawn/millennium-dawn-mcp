"""Settings loading: precedence of explicit argument, environment, and config file."""

from __future__ import annotations

import pytest

from md_mcp import config
from tests.support.mod import make_mod_root


def test_malformed_config_file_fails_loudly(tmp_path, monkeypatch):
    bad = tmp_path / "config.toml"
    bad.write_text("this is not = valid = toml", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", bad)
    with pytest.raises(RuntimeError, match="Could not load configuration file"):
        config._load_file_config()


def test_env_mod_root_wins_over_config_file(tmp_path, monkeypatch):
    env_root = make_mod_root(tmp_path / "EnvMod")
    file_root = make_mod_root(tmp_path / "FileMod")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'mod_root = "{file_root}"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setenv("MD_MOD_ROOT", str(env_root))
    assert config.load().mod_root == env_root


def test_explicit_mod_root_wins_over_env(tmp_path, monkeypatch):
    explicit_root = make_mod_root(tmp_path / "ExplicitMod")
    env_root = make_mod_root(tmp_path / "EnvMod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MOD_ROOT", str(env_root))
    assert config.load(str(explicit_root)).mod_root == explicit_root


def test_unrecognized_validator_mode_fails_loudly(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_VALIDATOR_MODE", "in-process")
    with pytest.raises(RuntimeError, match="in-process"):
        config.load(str(root))


@pytest.mark.parametrize(
    ("setting", "expected"),
    [("subprocess", "isolated"), ("isolated", "isolated"), ("in_process", "in_process")],
)
def test_load_resolves_the_subprocess_mode_alias(tmp_path, monkeypatch, setting, expected):
    root = make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_VALIDATOR_MODE", setting)
    assert config.load(str(root)).validator_mode == expected

    cfg = tmp_path / "config.toml"
    cfg.write_text(f'validator_mode = "{setting}"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.delenv("MD_MCP_VALIDATOR_MODE")
    assert config.load(str(root)).validator_mode == expected


def test_submod_root_loads_from_env_and_moves_default_cache(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    submod = tmp_path / "Overlay"
    submod.mkdir()
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("MD_MCP_SUBMOD_ROOT", str(submod))
    settings = config.load(str(root))
    assert vars(settings)["submod_root"] == submod.resolve()
    assert settings.cache_dir == submod / config.DEFAULT_CACHE_DIRNAME


def test_submod_root_loads_from_toml_without_mod_layout(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    submod = tmp_path / "OverlayOnly"
    submod.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'mod_root = "{root}"\nsubmod_root = "{submod}"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.delenv("MD_MCP_SUBMOD_ROOT", raising=False)
    settings = config.load()
    assert vars(settings)["submod_root"] == submod.resolve()


def test_invalid_submod_root_fails_loudly(tmp_path, monkeypatch):
    root = make_mod_root(tmp_path / "Mod")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    with pytest.raises(RuntimeError, match="existing directory"):
        config.load(str(root), **{"submod_root": str(tmp_path / "missing-overlay")})


@pytest.mark.parametrize("exists", [True, False])
def test_vanilla_path_is_set_only_when_hoi4_path_is_a_directory(tmp_path, monkeypatch, exists):
    root = make_mod_root(tmp_path / "Mod")
    vanilla = tmp_path / "Hearts of Iron IV"
    if exists:
        vanilla.mkdir()
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-such-config.toml")
    monkeypatch.setenv("HOI4_PATH", str(vanilla))

    assert config.load(str(root)).vanilla_path == (vanilla.resolve() if exists else None)
