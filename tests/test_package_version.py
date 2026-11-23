"""Verify the package version is consistent across pyproject.toml and md_mcp.__version__."""

from __future__ import annotations

import tomllib
from pathlib import Path

import md_mcp


def test_version_matches_pyproject() -> None:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    assert version == md_mcp.__version__
