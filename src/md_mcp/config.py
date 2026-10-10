"""Server configuration: mod-root, optional submod overlay, vanilla path, cache dir.

CLI flags > env vars > config file > defaults. Loaded once at startup and passed
into the index and tool layers.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .indexes.localisation import normalise_loc_langs
from .util.pathing import find_mod_root

CONFIG_PATH = Path.home() / ".config" / "md-mcp" / "config.toml"
DEFAULT_CACHE_DIRNAME = ".md-mcp-cache"
VALIDATOR_MODES = {"isolated", "in_process", "subprocess"}  # subprocess = alias for isolated


@dataclass
class Settings:
    mod_root: Path
    vanilla_path: Optional[Path]
    cache_dir: Path
    validator_mode: str = "isolated"  # or "in_process"
    default_lang: str = "en"
    submod_root: Optional[Path] = None
    # ISO codes of the loc languages to index. Empty means "just `default_lang`".
    # English is always included: every miss falls back to it, and an unindexed
    # fallback would cost a serial scan of the whole English tree per server start.
    loc_langs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        langs = tuple(self.loc_langs) or (self.default_lang.lower(),)
        if "en" not in langs:
            langs = (*langs, "en")
        self.loc_langs = langs


def load(mod_root: str | Path | None = None, submod_root: str | Path | None = None) -> Settings:
    """Resolve all settings. Explicit roots take precedence over env/config."""
    file_cfg = _load_file_config()

    root = find_mod_root(mod_root or os.environ.get("MD_MOD_ROOT") or file_cfg.get("mod_root"))

    submod_setting = (
        submod_root or os.environ.get("MD_MCP_SUBMOD_ROOT") or file_cfg.get("submod_root")
    )
    submod: Optional[Path] = None
    if submod_setting:
        candidate = Path(submod_setting).expanduser().resolve()
        if not candidate.is_dir():
            raise RuntimeError(f"submod_root must be an existing directory: {candidate}")
        submod = candidate

    # Vanilla support is opt-in. Reason: indexing vanilla doubles cold-build time and
    # forces a full reparse if the cache was built without it. Users rarely need
    # vanilla content for mod work — and when they do, they set `HOI4_PATH` or
    # `hoi4_path` in the config file explicitly.
    vanilla_setting = os.environ.get("HOI4_PATH") or file_cfg.get("hoi4_path")
    if vanilla_setting:
        candidate = Path(vanilla_setting).expanduser().resolve()
        v: Optional[Path] = candidate if candidate.is_dir() else None
    else:
        v = None

    cache_setting = os.environ.get("MD_MCP_CACHE_DIR") or file_cfg.get("cache_dir")
    cache_dir = (
        Path(cache_setting).expanduser().resolve()
        if cache_setting
        else (submod or root) / DEFAULT_CACHE_DIRNAME
    )

    validator_mode = os.environ.get("MD_MCP_VALIDATOR_MODE") or file_cfg.get(
        "validator_mode", "isolated"
    )
    if validator_mode not in VALIDATOR_MODES:
        raise RuntimeError(
            f"Invalid validator_mode {validator_mode!r}. Must be one of: "
            f"{', '.join(sorted(VALIDATOR_MODES))}"
        )
    if validator_mode == "subprocess":
        validator_mode = "isolated"

    default_lang = os.environ.get("MD_MCP_DEFAULT_LANG") or file_cfg.get("default_lang", "en")
    loc_langs_setting = os.environ.get("MD_MCP_LOC_LANGS") or file_cfg.get("loc_langs")
    try:
        # An explicit loc_langs must be valid; a derived one (just default_lang) is
        # lenient so an unknown MD_MCP_DEFAULT_LANG cannot stop the server starting.
        loc_langs = normalise_loc_langs(
            loc_langs_setting, default=default_lang, strict=loc_langs_setting is not None
        )
    except (TypeError, ValueError) as e:
        raise RuntimeError(f"Invalid loc_langs: {e}") from e
    if not loc_langs:
        # default_lang is not a known code: index English rather than nothing.
        loc_langs = ("en",)

    return Settings(
        mod_root=root,
        vanilla_path=v,
        cache_dir=cache_dir,
        submod_root=submod,
        validator_mode=validator_mode,
        default_lang=default_lang,
        loc_langs=loc_langs,
    )


def _load_file_config() -> dict:
    """Load ~/.config/md-mcp/config.toml when present.

    Raises RuntimeError when an existing config file cannot be read or parsed.
    """
    if not CONFIG_PATH.exists():
        return {}
    try:
        tomllib = importlib.import_module("tomllib")
    except ModuleNotFoundError:
        tomllib = importlib.import_module("tomli")
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Could not load configuration file {CONFIG_PATH}: {e}") from e
