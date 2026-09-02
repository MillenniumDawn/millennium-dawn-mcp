"""Verify the package version is consistent across pyproject.toml and md_mcp.__version__."""

from __future__ import annotations

import importlib
from pathlib import Path

import md_mcp


def test_version_matches_pyproject() -> None:
    try:
        tomllib = importlib.import_module("tomllib")
    except ModuleNotFoundError:
        tomllib = importlib.import_module("tomli")
    with Path("pyproject.toml").open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    assert version == md_mcp.__version__
