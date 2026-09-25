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

    return Settings(
        mod_root=root,
        vanilla_path=v,
        cache_dir=cache_dir,
        submod_root=submod,
        validator_mode=validator_mode,
        default_lang=os.environ.get("MD_MCP_DEFAULT_LANG") or file_cfg.get("default_lang", "en"),
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
